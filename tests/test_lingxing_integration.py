from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app import create_app
from batch_strategies import build_product_scenario
from traceability.db import connect_database
from traceability.lingxing import LingxingCredentials


class FakeLingxingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.receipts: list[dict] = []

    def credentials(self) -> LingxingCredentials:
        return LingxingCredentials("test-app", "test-secret", "test-id")

    def push(self, operation: str, payload: dict) -> dict:
        self.calls.append((operation, payload))
        if operation == "receipt_list":
            # 查询收货单列表 (getOrderList) returns pending 收货单 as data.list.
            return {
                "code": 0,
                "message": "success",
                "error_details": [],
                "data": {"total": len(self.receipts), "list": list(self.receipts)},
                "total": 0,
            }
        if operation == "inventory_sync":
            # 快捷入库 (fastReceive) answers with an empty data list on success.
            return {"code": 0, "message": "success", "error_details": [], "data": [], "total": 0}
        ids = {
            "purchase_order": {"poId": "LX-PO-1001"},
            "inbound_receipt": {"inboundId": "LX-IN-1001"},
        }
        return {"ok": True, "data": ids[operation]}


def test_lingxing_and_factory_end_to_end_with_mock_client() -> None:
    """Representative cross-service chain; no real Lingxing request is made."""

    workdir = Path(tempfile.mkdtemp(prefix="pts-lingxing-e2e-"))
    db_path = workdir / "traceability.db"
    fake = FakeLingxingService()
    try:
        app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(db_path),
                "LINGXING_SERVICE_FACTORY": lambda _database: fake,
            }
        )
        client = app.test_client()
        scenario = build_product_scenario(client, "LX-E2E", [1], 100)

        created_po = client.post(
            "/api/purchase-orders",
            json={
                "supplierId": scenario["suppliers"][0]["id"],
                "partTypeId": scenario["parts"][0]["id"],
                "quantity": 10,
            },
        )
        assert created_po.status_code == 201, created_po.get_json()
        purchase_order = created_po.get_json()["data"]
        assert purchase_order["syncStatus"] == "PENDING"

        created_receipt = client.post(
            "/api/inbound-receipts",
            json={"purchaseOrderId": purchase_order["id"], "quantity": 10},
        )
        assert created_receipt.status_code == 201, created_receipt.get_json()
        receipt = created_receipt.get_json()["data"]
        assert receipt["syncStatus"] == "PENDING"

        pushed_po = client.post(f"/api/purchase-orders/{purchase_order['id']}/push")
        assert pushed_po.status_code == 200, pushed_po.get_json()
        assert pushed_po.get_json()["data"]["lingxingPoId"] == "LX-PO-1001"

        pushed_receipt = client.post(f"/api/inbound-receipts/{receipt['id']}/push")
        assert pushed_receipt.status_code == 200, pushed_receipt.get_json()
        assert pushed_receipt.get_json()["data"]["lingxingInboundId"] == "LX-IN-1001"

        production = client.post(
            "/api/production-orders",
            json={"purchaseOrderId": purchase_order["id"]},
        )
        assert production.status_code == 201, production.get_json()
        production_order = production.get_json()["data"]
        assert production_order["productModelId"] == scenario["product"]["id"]
        assert len(production_order["products"]) == 1

        lookup = client.post(
            "/api/scan-gun/lookup",
            json={"code": production_order["identificationCode"]},
        )
        assert lookup.status_code == 200, lookup.get_json()
        assert lookup.get_json()["data"]["id"] == production_order["id"]

        inbound = client.post(
            "/api/scan-gun/inbound",
            json={"productionOrderId": production_order["id"], "quantity": 7},
        )
        assert inbound.status_code == 201, inbound.get_json()
        assert inbound.get_json()["data"]["onHand"] == 7

        progress = client.get(
            f"/api/purchase-orders/{purchase_order['id']}/factory-progress"
        )
        assert progress.status_code == 200, progress.get_json()
        assert progress.get_json()["data"]["productionOrderGenerated"] is True
        assert progress.get_json()["data"]["latestQuantity"] == 7

        # 库存同步 resolves the product's pending 收货单 (getOrderList) then
        # fast-receives it.
        fake.receipts.append(
            {
                "order_sn": "CR-LX-E2E",
                "item_list": [
                    {
                        "item_id": "1",
                        "sku": scenario["product"]["productCode"],
                        "notice_num_total": 7,
                        "product_receive_num": 0,
                    }
                ],
            }
        )
        stock_sync = client.post("/api/inventory-sync")
        assert stock_sync.status_code == 200, stock_sync.get_json()
        assert stock_sync.get_json()["data"]["itemCount"] == 1

        assert [operation for operation, _payload in fake.calls] == [
            "purchase_order",
            "inbound_receipt",
            "receipt_list",
            "inventory_sync",
        ]

        connection = connect_database(db_path)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
            assert connection.execute(
                "SELECT COUNT(*) AS n FROM audit_events WHERE object_type IN "
                "('PURCHASE_ORDER', 'INBOUND_RECEIPT', 'PRODUCTION_ORDER', 'INVENTORY_SYNC')"
            ).fetchone()["n"] >= 6
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

