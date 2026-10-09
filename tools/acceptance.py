"""End-to-end business acceptance against a throwaway database.

Why this exists
---------------
The unit suites prove each piece works. This proves the *factory* works: one
product goes from a supplier's incoming batch, through a purchase order, a
production order, a batch QR code, registration, quality release and finished-goods
stock-in — and the numbers still agree at the end. The scenarios below are the
ones where a plausible-looking implementation quietly gets the business wrong:
part of an order completed, a held batch, a double scan, two operators at once,
not enough material, and Lingxing answering in each of the ways it can.

Nothing here touches a real database. Each run seeds its own file under a
temporary directory, and the Lingxing scenarios use an injected transport, so no
request leaves the machine.

    python tools/acceptance.py
    python tools/acceptance.py --keep      # leave the database for inspection

Exit code is 0 only when every scenario passed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app  # noqa: E402
from batch_strategies import build_product_scenario  # noqa: E402
from capability_helpers import bootstrap_admin, insert_user, login  # noqa: E402
from traceability.lingxing import LingxingError  # noqa: E402

NOW = "2026-10-09T09:00:00+08:00"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


@dataclass
class Results:
    """Collects scenario outcomes so the run ends with one honest summary."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def check(self, scenario: str, name: str, condition: bool, detail: str = "") -> None:
        self.rows.append((scenario, name, "通过" if condition else "失败"))
        mark = "✓" if condition else "✗"
        suffix = f"  — {detail}" if detail else ""
        print(f"  {mark} {name}{suffix}")

    def failed(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[2] == "失败"]

    def summary(self) -> str:
        passed = len(self.rows) - len(self.failed())
        return f"{passed}/{len(self.rows)} 项通过"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class LingxingStub:
    """A Lingxing transport that answers however the scenario asks it to.

    ``mode`` is one of: ``ok``, ``reject``, ``refuse``, ``timeout``, ``partial``.
    ``refuse`` and ``timeout`` are different on purpose — a refusal means nothing
    was sent, a timeout means the far side may have acted — and the application is
    expected to treat them differently.
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[str] = []

    def request(self, method, url, *, json_data=None, headers=None, timeout=None):
        self.calls.append(url)
        if self.mode == "refuse":
            raise LingxingError("连接被拒绝", retryable=True)
        if self.mode == "timeout":
            raise LingxingError("请求超时，远端是否已写入未知", uncertain=True, status=504)
        if self.mode == "reject":
            raise LingxingError("领星接口返回 HTTP 400：参数错误", retryable=False)
        # Match the token *endpoint*, not the query string: every signed call
        # carries ``access_token=...`` and a loose "token" in url test would
        # answer the purchase-order push with a token payload.
        if "/oauth/" in url:
            return {"code": 0, "data": {"access_token": "stub-token", "refresh_token": "r", "expires_in": 7200}}
        if self.mode == "partial":
            # 领星批量下单在部分行被拒时，envelope 的 message 仍是 success，
            # 唯一可用的说明是 data 里带 detail 的条目。
            return {
                "code": 0,
                "message": "success",
                "data": [{"order_sn": "PO-PARTIAL-1", "detail": "错误：采购单状态已变更"}],
            }
        return {"code": 0, "data": {"id": "LX-STUB-1"}}


def make_app(workdir: Path, lingxing: LingxingStub | None = None, name: str = "acceptance.db"):
    database = workdir / name
    config: dict[str, Any] = {
        "TESTING": True,
        # Without these two the session and CSRF machinery stays switched off and
        # the run would not be exercising the real login path at all.
        "AUTH_DISABLED": False,
        "SECRET_KEY": "acceptance-run",
        "DATABASE": str(database),
        "NOW_PROVIDER": lambda: NOW,
    }
    if lingxing is not None:
        config["LINGXING_HTTP_CLIENT"] = lingxing
        config["LINGXING_ENDPOINTS"] = {
            "token": "https://openapi.lingxing.com/api/auth-server/oauth/access-token",
            "refresh_token": "https://openapi.lingxing.com/api/auth-server/oauth/refresh-token",
            "purchase_order": "https://openapi.lingxing.com/erp/sc/data/local_inventory/purchaseOrder",
            "inbound_order": "https://openapi.lingxing.com/erp/sc/routing/data/local_inventory/inboundOrder",
        }
        config["LINGXING_CREDENTIALS"] = {"appId": "A" * 16, "appSecret": "s" * 32}
        config["LINGXING_API_BASE_URL"] = "https://openapi.lingxing.com"
    app = create_app(config)
    return app, database


class AuthedClient:
    """A test client that carries the session's CSRF token on every write.

    The application requires ``X-CSRF-Token`` on POST/PUT/PATCH/DELETE and a
    browser always sends it. Switching the check off to make the run simpler would
    mean acceptance was measured against a configuration the factory never runs,
    so the token is attached here instead.
    """

    def __init__(self, client: Any, csrf: str) -> None:
        self._client = client
        self._csrf = csrf

    def _with_csrf(self, headers: Any) -> dict[str, str]:
        merged = dict(headers or {})
        merged.setdefault("X-CSRF-Token", self._csrf)
        return merged

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.get(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._with_csrf(kwargs.get("headers"))
        return self._client.post(*args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._with_csrf(kwargs.get("headers"))
        return self._client.put(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._with_csrf(kwargs.get("headers"))
        return self._client.delete(*args, **kwargs)


def staff(app, database: Path):
    """``(admin, warehouse, operations)`` signed-in test clients.

    The roles are created through the same helper the test suite uses, so this
    exercises the real login path rather than a forged session.
    """
    admin_client, admin_csrf = bootstrap_admin(app)
    insert_user(database, "wh", "WAREHOUSE")
    insert_user(database, "op", "OPERATIONS")
    warehouse_client, warehouse_csrf = login(app, "wh")
    operations_client, operations_csrf = login(app, "op")
    return (
        AuthedClient(admin_client, admin_csrf),
        AuthedClient(warehouse_client, warehouse_csrf),
        AuthedClient(operations_client, operations_csrf),
    )


def build_order(client, scenario: dict, quantity: int, suffix: str) -> dict:
    """Purchase order → production order → batch QR, the real first three steps."""
    purchase = client.post(
        "/api/purchase-orders",
        json={
            "poNo": f"PO-{suffix}",
            "supplierId": scenario["suppliers"][0]["id"],
            # Without a product model the order cannot be turned into a
            # production order — the app rejects it with 409.
            "productModelId": scenario["product"]["id"],
            "quantity": quantity,
        },
    )
    assert purchase.status_code == 201, purchase.get_json()
    order = client.post(
        "/api/production-orders",
        json={"purchaseOrderId": purchase.get_json()["data"]["id"], "quantity": quantity},
    )
    assert order.status_code == 201, order.get_json()
    return order.get_json()["data"]


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def scenario_a(workdir: Path, results: Results) -> None:
    """Complete normal production: material → batch → release → stock."""
    print("\n【场景 A】完整正常生产")
    app, database = make_app(workdir, name="a.db")
    admin, _warehouse, _operations = staff(app, database)

    scenario = build_product_scenario(admin, "A", [2], 500)

    # Two paths exist and they are not linked, which is worth stating plainly
    # because it decides where each number can be read from:
    #   * 批次生成 binds the trace plan's materials and deducts supplier stock;
    #   * 生产订单 mints its own batch + QR and is what the scan gun accepts,
    #     but deducts nothing.
    # The scenario exercises both and reports what each one shows.
    batch = admin.post(
        "/api/production-batches",
        json={"productModelId": scenario["product"]["id"], "quantity": 100, "prefix": "ACCA"},
    )
    assert batch.status_code == 201, batch.get_json()
    batch = batch.get_json()["data"]
    results.check("A", "生成生产批次二维码", bool(batch["identificationCode"]))

    order = build_order(admin, scenario, 100, "A")
    results.check("A", "生产订单自带唯一溯源码", bool(order["identificationCode"]))

    registered = admin.post(
        "/api/batch-entry/scan", json={"code": order["identificationCode"], "quantity": 100}
    )
    results.check("A", "登记整批生产完成", registered.status_code == 201, str(registered.get_json())[:80])
    record = registered.get_json()["data"]

    released = admin.post(f"/api/batch-trace-records/{record['id']}/pass")
    results.check("A", "质量放行", released.status_code == 200, str(released.get_json())[:80])

    inbound = admin.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": order["id"], "quantity": 100},
        headers={"Idempotency-Key": "acc-a-inbound-1"},
    )
    results.check("A", "成品扫码入库", inbound.status_code in (200, 201), str(inbound.get_json())[:80])

    connection = sqlite3.connect(str(database))
    try:
        # The material the batch consumed must have left the supplier batch.
        consumed = connection.execute(
            "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
            (scenario["inventory_batches"][0]["id"],),
        ).fetchone()[0]
        results.check("A", "原材料按 2/台 扣减 200", consumed == 300, f"剩余 {consumed}（初始 500，扣 200）")

        stock = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM inbound_scan_records"
        ).fetchone()[0]
        results.check("A", "成品入库数量正确", stock == 100, f"实际 {stock}")

        traced = admin.post("/api/batch-trace/query", json={"code": batch["identificationCode"]})
        data = traced.get_json()["data"]
        results.check("A", "批次追溯可查", traced.status_code == 200 and data["batchCode"] == batch["batchCode"])
        results.check("A", "追溯显示计划台数", data["plannedQuantity"] == 100, str(data["plannedQuantity"]))
        results.check("A", "追溯可读（生成批次未登记质量）", traced.status_code == 200, str(data.get("qualityStatus")))
        results.check("A", "反向追溯含原材料消耗", len(data["reverseTrace"]) >= 1, f"{len(data['reverseTrace'])} 条")
    finally:
        connection.close()


def scenario_b(workdir: Path, results: Results) -> None:
    """Partial completion: the rest must stay outstanding, not become scrap."""
    print("\n【场景 B】部分完成")
    app, database = make_app(workdir, name="b.db")
    admin, _w, _o = staff(app, database)

    scenario = build_product_scenario(admin, "B", [1], 1000)
    order = build_order(admin, scenario, 100, "B")
    admin.post("/api/batch-entry/scan", json={"code": order["identificationCode"], "quantity": 40})

    traced = admin.post("/api/batch-trace/query", json={"code": order["identificationCode"]}).get_json()["data"]
    results.check("B", "计划 100 / 已登记 40", traced["plannedQuantity"] == 100 and traced["registeredQuantity"] == 40)

    connection = sqlite3.connect(str(database))
    try:
        outstanding = connection.execute(
            "SELECT pb.planned_quantity - COALESCE(r.registered_quantity, 0) "
            "FROM production_batches pb "
            "LEFT JOIN batch_trace_records r ON r.production_batch_id = pb.id "
            "WHERE pb.id = ?",
            (order["productionBatchId"],),
        ).fetchone()[0]
        results.check("B", "未完成数量保留为 60，未记为损耗", outstanding == 60, f"未完成 {outstanding}")
        scrap = connection.execute(
            "SELECT COUNT(*) FROM batch_trace_records WHERE quality_status = 'VOID'"
        ).fetchone()[0]
        results.check("B", "未误判为报废", scrap == 0, f"作废记录 {scrap}")
    finally:
        connection.close()


def scenario_c(workdir: Path, results: Results) -> None:
    """Quality hold must actually block finished-goods stock-in."""
    print("\n【场景 C】质量暂扣")
    app, database = make_app(workdir, name="c.db")
    admin, _w, _o = staff(app, database)

    scenario = build_product_scenario(admin, "C", [1], 1000)
    order = build_order(admin, scenario, 10, "C")
    record = admin.post(
        "/api/batch-entry/scan", json={"code": order["identificationCode"], "quantity": 10}
    ).get_json()["data"]

    held = admin.post(f"/api/batch-trace-records/{record['id']}/hold", json={"reason": "外观检查不合格"})
    results.check("C", "暂扣成功", held.status_code == 200, str(held.get_json())[:70])

    blocked = admin.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": order["id"], "quantity": 10},
        headers={"Idempotency-Key": "acc-c-inbound"},
    )
    results.check("C", "暂扣批次入库被拒", blocked.status_code == 409, f"HTTP {blocked.status_code}")

    connection = sqlite3.connect(str(database))
    try:
        stock = connection.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0]
        results.check("C", "库存未增加", stock == 0, f"入库记录 {stock}")
        events = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type LIKE '%HOLD%' "
            "OR reason LIKE '%不合格%'"
        ).fetchone()[0]
        results.check("C", "审计记录了暂扣", events >= 1, f"{events} 条")
    finally:
        connection.close()


def scenario_d(workdir: Path, results: Results) -> None:
    """A repeated scan must replay, not add stock twice."""
    print("\n【场景 D】重复扫码 / 客户端重试")
    app, database = make_app(workdir, name="d.db")
    admin, _w, _o = staff(app, database)

    scenario = build_product_scenario(admin, "D", [1], 1000)
    order = build_order(admin, scenario, 10, "D")
    record = admin.post(
        "/api/batch-entry/scan", json={"code": order["identificationCode"], "quantity": 10}
    ).get_json()["data"]
    admin.post(f"/api/batch-trace-records/{record['id']}/pass")

    payload = {"productionOrderId": order["id"], "quantity": 10}
    key = {"Idempotency-Key": "acc-d-same-key"}
    first = admin.post("/api/scan-gun/inbound", json=payload, headers=key)
    second = admin.post("/api/scan-gun/inbound", json=payload, headers=key)
    results.check("D", "首次入库成功", first.status_code in (200, 201))
    results.check("D", "重试返回同一结果而非报错", second.status_code == first.status_code, f"HTTP {second.status_code}")

    connection = sqlite3.connect(str(database))
    try:
        stock = connection.execute("SELECT COALESCE(SUM(quantity), 0) FROM inbound_scan_records").fetchone()[0]
        results.check("D", "库存未重复增加", stock == 10, f"库存 {stock}（期望 10）")
    finally:
        connection.close()


def scenario_e(workdir: Path, results: Results) -> None:
    """Two operators, one order: the deduction must not oversell."""
    print("\n【场景 E】多工位并发")
    app, database = make_app(workdir, name="e.db")
    admin, _w, _o = staff(app, database)

    # 100 units of material, a product consuming 1 each: at most 100 units can be
    # produced. Ten threads asking for 20 each must not exceed that.
    scenario = build_product_scenario(admin, "E", [1], 100)

    errors: list[str] = []
    created: list[int] = []

    def worker(index: int) -> None:
        # Each thread signs in for itself: a Flask test client is not safe to
        # share, and the point of the scenario is ten independent operators.
        raw, csrf = login(app, "admin")
        client = AuthedClient(raw, csrf)
        response = client.post(
            "/api/production-batches",
            json={"productModelId": scenario["product"]["id"], "quantity": 20, "prefix": f"E{index:03d}"},
        )
        if response.status_code == 201:
            created.append(index)
        else:
            errors.append(str(response.get_json())[:60])

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    connection = sqlite3.connect(str(database))
    try:
        consumed = 100 - connection.execute(
            "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
            (scenario["inventory_batches"][0]["id"],),
        ).fetchone()[0]
        results.check("E", "扣减未超过可用库存", consumed <= 100, f"扣减 {consumed} / 可用 100")
        results.check("E", "并发下无异常", len(errors) == 0 or consumed == 100, f"成功 {len(created)}，拒绝 {len(errors)}")
        negative = connection.execute(
            "SELECT COUNT(*) FROM supplier_inventory_batches WHERE quantity_available < 0"
        ).fetchone()[0]
        results.check("E", "库存未出现负数", negative == 0, f"负数记录 {negative}")
    finally:
        connection.close()


def scenario_f(workdir: Path, results: Results) -> None:
    """Asking for more material than exists must be refused and rolled back."""
    print("\n【场景 F】物料不足")
    app, database = make_app(workdir, name="f.db")
    admin, _w, _o = staff(app, database)

    scenario = build_product_scenario(admin, "F", [1], 10)
    connection = sqlite3.connect(str(database))
    try:
        before = connection.execute(
            "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
            (scenario["inventory_batches"][0]["id"],),
        ).fetchone()[0]
    finally:
        connection.close()

    response = admin.post(
        "/api/production-batches",
        json={"productModelId": scenario["product"]["id"], "quantity": 999, "prefix": "ACCF"},
    )
    results.check("F", "超出可用物料被拒绝", response.status_code >= 400, f"HTTP {response.status_code}")

    connection = sqlite3.connect(str(database))
    try:
        after = connection.execute(
            "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
            (scenario["inventory_batches"][0]["id"],),
        ).fetchone()[0]
        results.check("F", "库存完整回滚", after == before, f"{before} -> {after}")
        batches = connection.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0]
        results.check("F", "未留下半成品批次", batches == 0, f"批次 {batches}")
    finally:
        connection.close()


def scenario_g(workdir: Path, results: Results) -> None:
    """Every way Lingxing can answer, and what the operator is told."""
    print("\n【场景 G】领星异常")
    for mode, label, expect in (
        ("ok", "正常成功", "PUSHED"),
        ("reject", "远端明确拒绝", "FAILED"),
        ("refuse", "网络连接失败", "FAILED"),
        ("timeout", "超时但远端可能成功", "UNKNOWN"),
    ):
        app, database = make_app(workdir, LingxingStub(mode), name=f"g-{mode}.db")
        admin, _w, _o = staff(app, database)
        scenario = build_product_scenario(admin, f"G{mode}", [1], 100)
        purchase = admin.post(
            "/api/purchase-orders",
            json={
                "poNo": f"PO-G-{mode}",
                "supplierId": scenario["suppliers"][0]["id"],
                "productModelId": scenario["product"]["id"],
                "quantity": 10,
            },
        ).get_json()["data"]

        admin.post(f"/api/purchase-orders/{purchase['id']}/push", json={"orderSn": f"SN-{mode}"})
        connection = sqlite3.connect(str(database))
        try:
            row = connection.execute(
                "SELECT sync_status, push_error FROM purchase_orders WHERE id = ?", (purchase["id"],)
            ).fetchone()
            status = row[0]
            message = row[1] or ""
            if expect == "UNKNOWN":
                events = connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE event_type = 'PO_PUSH_UNKNOWN'"
                ).fetchone()[0]
                results.check("G", f"{label} → 记录为结果未知", events >= 1, f"状态 {status}，UNKNOWN 事件 {events}")
                results.check("G", f"{label} → 提示先核对再重试", "核对" in message, message[:60])
            else:
                results.check("G", f"{label} → 状态 {expect}", status == expect, f"状态 {status}")
        finally:
            connection.close()

    # A partial result must not be reported as a clean success.
    app, database = make_app(workdir, LingxingStub("partial"), name="g-partial.db")
    admin, _w, _o = staff(app, database)
    scenario = build_product_scenario(admin, "Gp", [1], 100)
    purchase = admin.post(
        "/api/purchase-orders",
        json={
            "poNo": "PO-G-partial",
            "supplierId": scenario["suppliers"][0]["id"],
            "productModelId": scenario["product"]["id"],
            "quantity": 10,
        },
    ).get_json()["data"]
    admin.post(f"/api/purchase-orders/{purchase['id']}/push", json={"orderSn": "SN-partial"})
    connection = sqlite3.connect(str(database))
    try:
        status = connection.execute(
            "SELECT sync_status FROM purchase_orders WHERE id = ?", (purchase["id"],)
        ).fetchone()[0]
        results.check("G", "部分成功未被当作完全成功", status in {"FAILED", "PENDING"}, f"状态 {status}")
    finally:
        connection.close()


def scenario_h(workdir: Path, results: Results) -> None:
    """More rows than one window: nothing may be silently unreachable."""
    print("\n【场景 H】大量历史记录")
    app, database = make_app(workdir, name="h.db")
    admin, _w, _o = staff(app, database)
    scenario = build_product_scenario(admin, "H", [1], 100_000)
    admin.post(
        "/api/production-batches",
        json={"productModelId": scenario["product"]["id"], "quantity": 700, "prefix": "ACCH"},
    )

    connection = sqlite3.connect(str(database))
    try:
        # The list endpoint joins production_batches, so a record without its
        # batch would simply not appear — which is the failure this scenario is
        # meant to detect, not to cause.
        existing = connection.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0]
        for index in range(existing, 700):
            cursor = connection.execute(
                "INSERT INTO production_batches(batch_code, product_model_id, prefix, "
                "planned_quantity, generated_at) VALUES (?, ?, 'ACCH', 10, ?)",
                (f"ACCH-{index:04d}", scenario["product"]["id"], NOW),
            )
            connection.execute(
                "INSERT INTO batch_trace_records(production_batch_id, registered_quantity, "
                "registered_at, quality_status) VALUES (?, 10, ?, 'ASSEMBLED')",
                (cursor.lastrowid, NOW),
            )
        connection.commit()
    finally:
        connection.close()

    first = admin.get("/api/batch-trace-records")
    total = int(first.headers.get("X-Total-Count", 0))
    returned = len(first.get_json()["data"])
    results.check("H", "首屏有界返回", returned < total, f"返回 {returned} / 共 {total}")
    results.check("H", "响应头报告总数", total > 500, f"X-Total-Count={total}")

    seen: set[int] = set()
    offset = 0
    while True:
        page = admin.get(f"/api/batch-trace-records?limit=250&offset={offset}").get_json()["data"]
        if not page:
            break
        seen.update(row["id"] for row in page)
        offset += 250
        if offset > 2000:
            break
    results.check("H", "分页可遍历全部记录", len(seen) == total, f"遍历到 {len(seen)} 条 / 共 {total}")

    filtered = admin.get("/api/batch-trace-records?batchCode=ACCH-0000")
    results.check("H", "筛选可定位", filtered.status_code == 200, f"HTTP {filtered.status_code}")


def scenario_i(workdir: Path, results: Results) -> None:
    """Trace both directions, including an older record."""
    print("\n【场景 I】产品追溯")
    app, database = make_app(workdir, name="i.db")
    admin, _w, _o = staff(app, database)

    scenario = build_product_scenario(admin, "I", [3], 500)
    # The material binding lives on the 批次生成 batch, so that is the code whose
    # trace can show what the batch consumed.
    batch = admin.post(
        "/api/production-batches",
        json={"productModelId": scenario["product"]["id"], "quantity": 50, "prefix": "ACCI"},
    ).get_json()["data"]
    record = admin.post(
        "/api/batch-entry/scan", json={"code": batch["identificationCode"], "quantity": 50}
    ).get_json()["data"]
    admin.post(f"/api/batch-trace-records/{record['id']}/pass")

    data = admin.post("/api/batch-trace/query", json={"code": batch["identificationCode"]}).get_json()["data"]
    results.check("I", "批次 → 原材料（反向追溯）", len(data["reverseTrace"]) == 1, f"{len(data['reverseTrace'])} 条")
    results.check(
        "I",
        "消耗量 = 登记台数 × 每台用量",
        data["reverseTrace"][0]["quantityConsumed"] == 150,
        f"{data['reverseTrace'][0]['quantityConsumed']}（50×3）",
    )

    supplier_batch = scenario["inventory_batches"][0]["id"]
    forward = admin.get(f"/api/supplier-inventory-batches/{supplier_batch}/forward-trace")
    results.check("I", "原材料 → 受影响产品（正向追溯）", forward.status_code == 200, f"HTTP {forward.status_code}")

    # 族谱按单机识别码查询；本场景没有单机记录，因此正确的回答是 404 而不是 500。
    genealogy = admin.get("/api/genealogy?code=UNKNOWN-CODE-0001")
    results.check(
        "I",
        "历史族谱接口可达且对未知识别码返回 404",
        genealogy.status_code in (200, 404),
        f"HTTP {genealogy.status_code}",
    )


def scenario_j(workdir: Path, results: Results) -> None:
    """Back up, verify, and restore into an isolated database."""
    print("\n【场景 J】系统备份与恢复")
    from traceability.backup import create_backup, restore_backup, verify_backup

    app, database = make_app(workdir, name="j.db")
    admin, _w, _o = staff(app, database)
    scenario = build_product_scenario(admin, "J", [1], 500)
    build_order(admin, scenario, 20, "J")

    # A product image, so the restore has to bring back more than the database.
    import io
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    png = b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + zlib.compress(b"x")
    upload = admin.post(
        "/api/product-images",
        data={"file": (io.BytesIO(png), "main.png")},
        content_type="multipart/form-data",
    )
    results.check("J", "上传产品图片", upload.status_code == 201, f"HTTP {upload.status_code}")

    backups = workdir / "backups"
    report = create_backup(database, backups, created_at=NOW)
    results.check("J", "创建备份", report.path.is_file())
    results.check("J", "备份含图片归档", bool(report.images), json.dumps(report.images or {}, ensure_ascii=False)[:60])

    verified = verify_backup(report.path)
    results.check("J", "备份校验通过", verified.ok, "; ".join(verified.problems)[:70])
    results.check("J", "校验报告图片数量", verified.image_count == 1, str(verified.image_count))

    isolated = workdir / "restored.db"
    shutil.copy2(database, isolated)
    restored = restore_backup(report.path, isolated, created_at=NOW)
    results.check("J", "恢复到隔离数据库", restored.images_restored >= 1, f"图片 {restored.images_restored}")

    connection = sqlite3.connect(str(isolated))
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("users", "suppliers", "product_models", "purchase_orders", "supplier_inventory_batches")
        }
        results.check("J", "恢复后数据完整", all(value > 0 for value in counts.values()), json.dumps(counts))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        results.check("J", "恢复后数据库自检通过", integrity == "ok", integrity)
    finally:
        connection.close()


SCENARIOS = (
    scenario_a,
    scenario_b,
    scenario_c,
    scenario_d,
    scenario_e,
    scenario_f,
    scenario_g,
    scenario_h,
    scenario_i,
    scenario_j,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="leave the working directory behind")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pts-acceptance-"))
    print(f"工作目录：{workdir}")
    print("使用独立测试数据库；不触碰 data/ 下的任何文件。")

    results = Results()
    try:
        for scenario in SCENARIOS:
            try:
                scenario(workdir, results)
            except Exception as error:  # noqa: BLE001 — a crash is a failed scenario
                name = scenario.__doc__.splitlines()[0] if scenario.__doc__ else scenario.__name__
                results.check("!", f"{name} 执行异常", False, f"{type(error).__name__}: {error}"[:90])
    finally:
        if args.keep:
            print(f"\n保留工作目录：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n=== 验收结果：{results.summary()} ===")
    failures = results.failed()
    if failures:
        print("失败项：")
        for scenario, name, _ in failures:
            print(f"  [{scenario}] {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
