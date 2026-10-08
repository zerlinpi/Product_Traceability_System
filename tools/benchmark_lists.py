"""Measure the list endpoints against a realistically sized database.

Why this exists
---------------
Several list endpoints load their whole table and hand it to the browser, and the
brief for this round asks for pagination. Before changing an API the front end
consumes as a plain array, it is worth knowing what the current shape actually
costs — the answer decides whether the change is worth its compatibility risk, and
gives a number to compare against afterwards.

Every run seeds its own database from a fixed seed, so the figures below are
reproducible rather than impressions:

    python tools/benchmark_lists.py
    python tools/benchmark_lists.py --scale 5      # five times the rows

Sizes are chosen to represent a factory a few years in: thousands of purchase and
production orders, tens of thousands of trace records. The numbers printed are
wall-clock milliseconds for the HTTP call through Flask's test client, plus the
response size, which is what the workstation actually has to parse.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app  # noqa: E402

#: Rows to seed at scale 1.
ROWS = {
    "purchase_orders": 2_000,
    "production_orders": 2_000,
    "inbound_receipts": 5_000,
    "batch_trace_records": 30_000,
    "supplier_inventory_batches": 3_000,
    "suppliers": 200,
    "product_models": 500,
}

#: The list endpoints to measure, with the table that drives their size.
ENDPOINTS = (
    ("/api/purchase-orders", "purchase_orders"),
    ("/api/production-orders", "production_orders"),
    ("/api/inbound-receipts", "inbound_receipts"),
    ("/api/batch-trace-records", "batch_trace_records"),
    ("/api/supplier-inventory-batches", "supplier_inventory_batches"),
    ("/api/suppliers", "suppliers"),
    ("/api/product-models", "product_models"),
    ("/api/records", "batch_trace_records"),
)


#: Export endpoints. These are the ones that build a whole payload in memory, so
#: they are measured for peak allocation rather than for bytes sent.
EXPORTS = (
    ("/api/records/export.xlsx", "Excel，最多 100000 条记录"),
    ("/api/products/1/qrcodes.zip", "ZIP，该产品的全部历史二维码"),
    ("/api/product-code-batches/1/qrcodes.zip", "ZIP，单个生成批次"),
    ("/api/purchase-orders/export", "Excel，领星 48 列模板"),
)


def seed(database: Path, scale: int) -> dict[str, int]:
    """Fill a migrated database with plausible rows. Returns what was inserted.

    Prerequisites are created first and given id 1: a product family, a supplier,
    a part type, a product model and a production batch. The volume tables then
    reference them, which is what a real database looks like after a few years —
    the point is to measure list endpoints against realistic row counts, not to
    exercise the write paths.
    """
    create_app({"TESTING": True, "DATABASE": str(database)})
    connection = sqlite3.connect(str(database))
    connection.execute("PRAGMA foreign_keys = ON")
    inserted: dict[str, int] = {}
    stamp = "2026-01-01T00:00:00"
    try:
        # Prerequisites are seeded too, so they have to be inspected as well.
        tables = set(ROWS) | {
            "product_families",
            "part_types",
            "production_batches",
        }
        columns = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }

        def insert(table: str, values: dict) -> None:
            keys = [key for key in values if key in columns[table]]
            placeholders = ", ".join("?" for _ in keys)
            connection.execute(
                f"INSERT INTO {table}({', '.join(keys)}) VALUES ({placeholders})",
                [values[key] for key in keys],
            )

        # --- prerequisites, all id 1 -------------------------------------
        insert(
            "product_families",
            {
                "product_code": "FAM-1",
                "name": "基准产品族",
                "active": 1,
                "created_at": stamp,
                "updated_at": stamp,
            },
        )
        insert(
            "suppliers",
            {
                "supplier_code": "SUP-BASE",
                "name": "基准供应商",
                "contact": "张三",
                "phone": "13800000000",
                "created_at": stamp,
                "updated_at": stamp,
            },
        )
        insert(
            "part_types",
            {
                "part_code": "PART-1",
                "name": "基准部件",
                "supplier_id": 1,
                "created_at": stamp,
                "updated_at": stamp,
            },
        )
        insert(
            "product_models",
            {
                "product_family_id": 1,
                "model_code": "TW-BASE",
                "name": "基准型号",
                "serial_prefix": "TW",
                "active": 1,
                "created_at": stamp,
                "updated_at": stamp,
            },
        )
        insert(
            "production_batches",
            {
                "batch_code": "PB-BASE",
                "product_model_id": 1,
                "prefix": "TW",
                "planned_quantity": 10_000,
                "generated_at": stamp,
            },
        )
        insert(
            "purchase_orders",
            {"po_no": "PO-BASE", "supplier_id": 1, "status": "DRAFT", "created_at": stamp},
        )

        # --- volume ------------------------------------------------------
        for index in range(ROWS["suppliers"] * scale):
            insert(
                "suppliers",
                {
                    "supplier_code": f"SUP-{index:06d}",
                    "name": f"供应商 {index:06d}",
                    "contact": "李四",
                    "phone": "13900000000",
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            )
        inserted["suppliers"] = ROWS["suppliers"] * scale

        for index in range(ROWS["product_models"] * scale):
            insert(
                "product_models",
                {
                    "product_family_id": 1,
                    "model_code": f"TW-{index:06d}",
                    "name": f"型号 {index:06d}",
                    "serial_prefix": "TW",
                    "active": 1,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "attributes_json": json.dumps(
                        {"主图": f"/api/product-images/m{index}.png", "备注": "性能测试"},
                        ensure_ascii=False,
                    ),
                },
            )
        inserted["product_models"] = ROWS["product_models"] * scale

        for index in range(ROWS["purchase_orders"] * scale):
            insert(
                "purchase_orders",
                {
                    "po_no": f"PO-{index:06d}",
                    "supplier_id": 1,
                    "status": "DRAFT",
                    "created_at": stamp,
                },
            )
        inserted["purchase_orders"] = ROWS["purchase_orders"] * scale

        # production_orders is unique on production_batch_id *and* on
        # purchase_order_id, and batch_trace_records is unique on
        # production_batch_id, so both need a batch each. Seed enough batches
        # for whichever of the two is larger.
        batch_count = max(
            ROWS["production_orders"] * scale,
            ROWS["batch_trace_records"] * scale,
        )
        for index in range(batch_count):
            insert(
                "production_batches",
                {
                    "batch_code": f"PB-{index:06d}",
                    "product_model_id": 1,
                    "prefix": "TW",
                    "planned_quantity": 10,
                    "generated_at": stamp,
                },
            )
        inserted["production_batches"] = batch_count

        for index in range(ROWS["production_orders"] * scale):
            insert(
                "production_orders",
                {
                    # Both references must be unique, so they move together.
                    "purchase_order_id": index + 1,
                    "production_batch_id": index + 1,
                    "status": "OPEN",
                    "created_at": stamp,
                },
            )
        inserted["production_orders"] = ROWS["production_orders"] * scale

        for index in range(ROWS["supplier_inventory_batches"] * scale):
            insert(
                "supplier_inventory_batches",
                {
                    "part_type_id": 1,
                    "batch_no": f"IB-{index:06d}",
                    "quantity_received": 100,
                    "quantity_available": 100,
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            )
        inserted["supplier_inventory_batches"] = ROWS["supplier_inventory_batches"] * scale

        for _ in range(ROWS["inbound_receipts"] * scale):
            insert(
                "inbound_receipts",
                {
                    "purchase_order_id": 1,
                    "quantity": 100,
                    "received_at": stamp,
                    "created_at": stamp,
                },
            )
        inserted["inbound_receipts"] = ROWS["inbound_receipts"] * scale

        for index in range(ROWS["batch_trace_records"] * scale):
            insert(
                "batch_trace_records",
                {
                    "production_batch_id": index + 1,
                    "registered_quantity": 10,
                    "registered_at": stamp,
                    "status": "ASSEMBLED",
                },
            )
        inserted["batch_trace_records"] = ROWS["batch_trace_records"] * scale

        connection.commit()
    finally:
        connection.close()
    return inserted


def measure(database: Path, rows: dict[str, int]) -> list[dict]:
    """Time each list endpoint and record how much it sends back."""
    app = create_app({"TESTING": True, "DATABASE": str(database)})
    client = app.test_client()

    # The endpoints require a signed-in operator; the bootstrap admin works.
    with client.session_transaction() as session:
        session["user_id"] = 1
        session["role"] = "ADMIN"
        session["username"] = "admin"

    results = []
    for url, table in ENDPOINTS:
        started = time.perf_counter()
        response = client.get(url)
        elapsed_ms = (time.perf_counter() - started) * 1000
        body = response.get_data()
        try:
            payload = response.get_json()
            count = len(payload.get("data") or []) if isinstance(payload, dict) else None
        except Exception:
            count = None
        results.append(
            {
                "url": url,
                "table": table,
                "rows": rows.get(table, 0),
                "status": response.status_code,
                "ms": elapsed_ms,
                "bytes": len(body),
                "returned": count,
                "total": response.headers.get("X-Total-Count"),
            }
        )
    return results


def measure_exports(database: Path, rows: dict[str, int]) -> list[dict]:
    """Time the export endpoints and record how much memory they hold.

    Exports build their whole payload before sending it — a ZIP or a workbook is
    written into a BytesIO. That is fine at a few hundred rows and not fine at a
    hundred thousand, so the peak traced allocation matters more here than the
    wall-clock time.

    ``tracemalloc`` sees Python allocations, which is what grows: the workbook,
    the archive, and the per-row dictionaries feeding them. It does not see the
    compressed bytes the C layer holds, so treat the figure as a lower bound.
    """
    import tracemalloc

    app = create_app({"TESTING": True, "DATABASE": str(database)})
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = 1
        session["role"] = "ADMIN"
        session["username"] = "admin"

    results = []
    for url, note in EXPORTS:
        tracemalloc.start()
        try:
            started = time.perf_counter()
            response = client.get(url)
            elapsed_ms = (time.perf_counter() - started) * 1000
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        results.append(
            {
                "url": url,
                "note": note,
                "status": response.status_code,
                "ms": elapsed_ms,
                "peak_mb": peak / 1024 / 1024,
                "bytes": len(response.get_data()),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, default=1, help="multiply every row count")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pts-bench-"))
    database = workdir / "bench.db"

    print(f"=== 播种数据（scale={args.scale}）===")
    rows = seed(database, args.scale)
    for table, count in sorted(rows.items()):
        print(f"  {table:<32} {count:>8,}")
    print(f"  数据库大小：{database.stat().st_size / 1024 / 1024:.1f} MB")

    print()
    print("=== 列表端点 ===")
    print(
        f"  {'端点':<36} {'状态':>4} {'总行数':>8} {'耗时':>10} {'响应':>10} {'本次返回':>8}"
    )
    results = measure(database, rows)
    for item in results:
        total = item["total"] or "-"
        print(
            f"  {item['url']:<36} {item['status']:>4} {item['rows']:>8,} "
            f"{item['ms']:>8.0f} ms {item['bytes'] / 1024:>8.0f} KB "
            f"{item['returned'] if item['returned'] is not None else '-':>8}"
            f"  / 共 {total}"
        )

    print()
    print("=== 导出端点（峰值内存）===")
    print(f"  {'端点':<44} {'状态':>4} {'耗时':>10} {'峰值内存':>10} {'产物':>10}")
    for item in measure_exports(database, rows):
        print(
            f"  {item['url']:<44} {item['status']:>4} {item['ms']:>8.0f} ms "
            f"{item['peak_mb']:>8.1f} MB {item['bytes'] / 1024 / 1024:>8.2f} MB"
        )
    print()
    print("  说明：导出把整个产物构建在内存里（ZIP / 工作簿写入 BytesIO）。")
    print("        峰值内存由 tracemalloc 统计 Python 侧分配，是下限。")

    print()
    print("  说明：耗时是 Flask 测试客户端的单次调用，不含网络。")
    print("        「本次返回 / 共」来自 X-Returned-Count 与 X-Total-Count 响应头；")
    print("        两者相差很大时，说明该表已超出单页窗口，调用方应分页或缩小筛选。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
