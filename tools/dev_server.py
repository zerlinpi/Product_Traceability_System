"""Local development / end-to-end server with a throwaway database.

    python tools/dev_server.py                 # fresh temp DB, seeded, port 5090
    python tools/dev_server.py --port 5091 --no-seed
    python tools/dev_server.py --db exports/dev.db   # keep the DB between runs

It never touches ``data/traceability.db`` unless ``--db`` points at it, and it
refuses to run with ``PTS_ENV=production``. Use it with the Vite dev server
(``pnpm dev`` in frontend/, which proxies /api to port 5080 by default — pass
``--port 5080`` or set ``VITE_DEV_API_TARGET``) or against the built
``static/dist`` bundle.

Seeded accounts (password already changed, no forced change on first login):

    admin            / Dev@Admin#2026
    warehouse.demo   / Dev@Store#2026
    operations.demo  / Dev@Ops#2026

Lingxing is not configured, so push buttons report the missing credentials —
exactly what a fresh installation does.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.security import generate_password_hash  # noqa: E402

from app import DEFAULT_BOOTSTRAP_PASSWORD, create_app  # noqa: E402
from traceability.db import connect_database  # noqa: E402

ADMIN_PASSWORD = "Dev@Admin#2026"  # noqa: S105 - local throwaway database only
DEMO_USERS = (
    ("warehouse.demo", "仓管演示", "WAREHOUSE", "Dev@Store#2026"),
    ("operations.demo", "运营演示", "OPERATIONS", "Dev@Ops#2026"),
)


def _post(client, csrf: str, path: str, payload: dict, expected: int = 201) -> dict:
    response = client.post(path, json=payload, headers={"X-CSRF-Token": csrf})
    body = response.get_json(silent=True) or {}
    if response.status_code != expected:
        raise RuntimeError(f"{path} -> {response.status_code}: {body.get('message')}")
    return body.get("data") or {}


def _insert_user(database_path: Path, username: str, display_name: str, role: str, password: str) -> None:
    database = connect_database(database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            INSERT OR IGNORE INTO users(
                username, display_name, password_hash, role, active,
                must_change_password, session_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 0, 1, datetime('now'), datetime('now'))
            """,
            (username, display_name, generate_password_hash(password), role),
        )
        database.execute("COMMIT")
    except Exception:
        database.execute("ROLLBACK")
        raise
    finally:
        database.close()


def seed(app, database_path: Path) -> None:
    client = app.test_client()
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": DEFAULT_BOOTSTRAP_PASSWORD}
    )
    if login.status_code != 200:
        print("seed: admin already initialised, skipping demo data")
        return
    csrf = login.get_json()["data"]["csrfToken"]
    changed = client.post(
        "/api/auth/change-password",
        json={"currentPassword": DEFAULT_BOOTSTRAP_PASSWORD, "newPassword": ADMIN_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    csrf = changed.get_json()["data"]["csrfToken"]
    for username, display_name, role, password in DEMO_USERS:
        _insert_user(database_path, username, display_name, role, password)

    supplier = _post(client, csrf, "/api/suppliers", {
        "supplierCode": "SUP-A01", "name": "华东电机", "contact": "张工", "phone": "13800000001",
    })
    motor = _post(client, csrf, "/api/part-types", {
        "partCode": "MOTOR-2HP", "name": "驱动电机", "supplierId": supplier["id"],
        "specification": "2.0HP 直流", "minimumStock": 50,
    })
    board = _post(client, csrf, "/api/part-types", {
        "partCode": "CTRL-V3", "name": "主控板", "supplierId": supplier["id"],
        "specification": "V3", "minimumStock": 20,
    })
    motor_lot = _post(client, csrf, "/api/supplier-inventory-batches", {
        "partTypeId": motor["id"], "quantity": 500, "batchNo": "LOT-M-0801",
        "productionDate": "20260801", "receivedDate": "20260805",
    })
    board_lot = _post(client, csrf, "/api/supplier-inventory-batches", {
        "partTypeId": board["id"], "quantity": 30, "batchNo": "LOT-C-0802",
        "productionDate": "20260802", "receivedDate": "20260806",
    })
    product = _post(client, csrf, "/api/products", {
        "name": "折叠走步机 标准版",
        "components": [
            {"inventoryBatchId": motor_lot["id"], "quantity": 1},
            {"inventoryBatchId": board_lot["id"], "quantity": 1},
        ],
    })
    finished = _post(client, csrf, "/api/products", {"name": "跑步机润滑油（整件外采）", "components": []})

    batch = _post(client, csrf, "/api/production-batches", {
        "productModelId": product["id"], "quantity": 12, "prefix": "TW",
    })
    _post(client, csrf, "/api/production-batches", {
        "productModelId": product["id"], "quantity": 6, "prefix": "TWB",
    })
    registered = _post(client, csrf, "/api/batch-entry/scan", {
        "code": batch["batchCode"], "stationId": "station-dev-seed", "stationName": "种子数据",
    })
    _post(client, csrf, f"/api/batch-trace-records/{registered['id']}/pass", {}, expected=200)

    order = _post(client, csrf, "/api/purchase-orders", {
        "productModelId": product["id"],
        "fields": {"SKU": "TW-FOLD-STD", "供应商": "华东电机", "实际采购量": 12, "含税单价": 1680, "采购币种": "CNY"},
    })
    _post(client, csrf, "/api/purchase-orders", {
        "productModelId": finished["id"],
        "fields": {"SKU": "LUBE-500ML", "实际采购量": 40, "含税单价": 18.5},
    })
    _post(client, csrf, "/api/production-orders/batch", {"purchaseOrderIds": [order["id"]]})
    print(f"seed: demo data created (production batch {batch['batchCode']})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=5090)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", type=Path, help="database file (default: a new temporary file)")
    parser.add_argument("--no-seed", action="store_true", help="start with only the bootstrap admin")
    args = parser.parse_args()

    if os.environ.get("PTS_ENV", "").strip().lower() == "production":
        print("dev_server.py refuses to run with PTS_ENV=production", file=sys.stderr)
        return 2

    if args.db:
        database_path = args.db.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        database_path = Path(tempfile.mkdtemp(prefix="pts-dev-")) / "traceability.db"
    if database_path == (ROOT / "data" / "traceability.db").resolve():
        print("warning: using the real data/traceability.db", file=sys.stderr)

    fresh = not database_path.exists()
    app = create_app({"DATABASE": str(database_path), "SECRET_KEY": "pts-local-dev-server-secret-key-000000"})
    if fresh and not args.no_seed:
        seed(app, database_path)
    print(f"database: {database_path}")
    print(f"serving:  http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
