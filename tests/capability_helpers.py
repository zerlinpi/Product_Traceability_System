from __future__ import annotations

from pathlib import Path

from werkzeug.security import generate_password_hash

from app import create_app
from traceability.db import connect_database
from traceability.lingxing import LingxingCredentials, LingxingError


PASSWORD = "RolePass@123"


class FakeLingxingService:
    def __init__(self, *, configured: bool = True, failures: set[str] | None = None) -> None:
        self.configured = configured
        self.failures = set(failures or ())
        self.calls: list[tuple[str, dict]] = []
        # Pending 收货单 rows returned by 查询收货单列表 (getOrderList), keyed by
        # nothing in particular — tests append via ``add_receipt``.
        self.receipts: list[dict] = []

    def add_receipt(
        self,
        order_sn: str,
        sku: str,
        notice_num_total: int,
        *,
        item_id: int = 1,
        product_receive_num: int = 0,
        business_order_sn: str = "",
    ) -> None:
        """Register a pending 收货单 line so 库存同步 can fast-receive it."""
        self.receipts.append(
            {
                "order_sn": order_sn,
                "business_order_sn": business_order_sn,
                "status": 10,
                "order_type": 1,
                "item_list": [
                    {
                        "item_id": str(item_id),
                        "sku": sku,
                        "product_name": sku,
                        "notice_num_total": notice_num_total,
                        "product_receive_num": product_receive_num,
                    }
                ],
            }
        )

    def credentials(self) -> LingxingCredentials:
        if not self.configured:
            raise LingxingError("领星凭据未配置", status=503)
        return LingxingCredentials("test-app", "test-secret", "test-account")

    def push(self, operation: str, payload: dict) -> dict:
        self.calls.append((operation, payload))
        if operation in self.failures:
            raise LingxingError(f"mock {operation} failed")
        if operation in {"purchase_order", "inventory_sync"}:
            # 采购单下单 (setOrders) and 快捷入库 (fastReceive) both transition an
            # existing order and answer with an empty data list on success, so no
            # object id comes back.
            return {
                "code": 0,
                "message": "success",
                "error_details": [],
                "data": [],
                "total": 0,
            }
        if operation == "receipt_list":
            # 查询收货单列表 (getOrderList): returns pending 收货单 as data.list.
            return {
                "code": 0,
                "message": "success",
                "error_details": [],
                "data": {"total": len(self.receipts), "list": list(self.receipts)},
                "total": 0,
            }
        values = {"inbound_receipt": {"inboundId": "LX-IN-1"}}
        return {"ok": True, "data": values[operation]}


def make_auth_app(tmp_path: Path, service: FakeLingxingService | None = None):
    database_path = tmp_path / "traceability.db"
    fake = service or FakeLingxingService()
    app = create_app(
        {
            "TESTING": True,
            "AUTH_DISABLED": False,
            "DATABASE": str(database_path),
            "SECRET_KEY": "capability-tests",
            "NOW_PROVIDER": lambda: "2026-07-22T10:30:00+08:00",
            "LINGXING_SERVICE_FACTORY": lambda _database: fake,
        }
    )
    return app, database_path, fake


def insert_user(database_path: Path, username: str, role: str, product_ids=()) -> int:
    database = connect_database(database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            INSERT INTO users(
                username, display_name, password_hash, role, active,
                must_change_password, session_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 0, 1, 'now', 'now')
            """,
            (username, username, generate_password_hash(PASSWORD), role),
        )
        for product_id in product_ids:
            database.execute(
                "INSERT INTO user_product_model_permissions(user_id, product_model_id) VALUES (?, ?)",
                (cursor.lastrowid, product_id),
            )
        database.execute("COMMIT")
        return int(cursor.lastrowid)
    except Exception:
        database.execute("ROLLBACK")
        raise
    finally:
        database.close()


def login(app, username: str):
    client = app.test_client()
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    return client, response.get_json()["data"]["csrfToken"]


def bootstrap_admin(app):
    client = app.test_client()
    first = client.post(
        "/api/auth/login", json={"username": "admin", "password": "Admin@12345"}
    )
    assert first.status_code == 200, first.get_json()
    changed = client.post(
        "/api/auth/change-password",
        json={"currentPassword": "Admin@12345", "newPassword": PASSWORD},
        headers={"X-CSRF-Token": first.get_json()["data"]["csrfToken"]},
    )
    assert changed.status_code == 200, changed.get_json()
    return client, changed.get_json()["data"]["csrfToken"]


def post(client, csrf: str, path: str, payload: dict, expected: int = 201):
    response = client.post(path, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == expected, response.get_json()
    return response.get_json()["data"]


def seed_scenario(admin, csrf: str, suffix: str = "CAP") -> dict:
    supplier = post(
        admin,
        csrf,
        "/api/suppliers",
        {"supplierCode": f"SUP-{suffix}", "name": f"供应商 {suffix}", "contact": "张三", "phone": "13800000000"},
    )
    part = post(
        admin,
        csrf,
        "/api/part-types",
        {
            "partCode": f"PART-{suffix}",
            "name": f"部件 {suffix}",
            "supplierId": supplier["id"],
            "specification": "标准型",
        },
    )
    inventory = post(
        admin,
        csrf,
        "/api/supplier-inventory-batches",
        {
            "partTypeId": part["id"],
            "quantity": 1_000_000,
            "batchNo": f"LOT-{suffix}",
            "productionDate": "20260720",
            "receivedDate": "20260721",
        },
    )
    product = post(
        admin,
        csrf,
        "/api/products",
        {
            "name": f"产品 {suffix}",
            "components": [{"inventoryBatchId": inventory["id"], "quantity": 1}],
        },
    )
    return {"supplier": supplier, "part": part, "inventory": inventory, "product": product}


def create_po(client, csrf: str, scenario: dict, quantity: int = 10):
    return post(
        client,
        csrf,
        "/api/purchase-orders",
        {
            "supplierId": scenario["supplier"]["id"],
            "partTypeId": scenario["part"]["id"],
            "quantity": quantity,
        },
    )

