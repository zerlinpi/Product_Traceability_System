from __future__ import annotations

import pytest

from app import create_app
from batch_strategies import build_product_scenario
from capability_helpers import FakeLingxingService
from traceability.db import connect_database


def setup_case(tmp_path):
    database_path = tmp_path / "purchase-orders.db"
    fake = FakeLingxingService()
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "NOW_PROVIDER": lambda: "2026-07-22T10:30:00+08:00",
            "LINGXING_SERVICE_FACTORY": lambda _database: fake,
        }
    )
    client = app.test_client()
    scenario = build_product_scenario(client, "PO", [1], 100)
    return client, database_path, scenario, fake


# Feature: batch-traceability, Property 42: 采购订单创建保真且初始化为待推送
@pytest.mark.parametrize("quantity", [1, 2, 999999])
def test_purchase_order_creation_is_faithful_and_pending(tmp_path, quantity):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    response = client.post(
        "/api/purchase-orders",
        json={
            "supplierId": scenario["suppliers"][0]["id"],
            "partTypeId": scenario["parts"][0]["id"],
            "quantity": quantity,
        },
    )
    assert response.status_code == 201, response.get_json()
    order = response.get_json()["data"]
    assert order["supplierId"] == scenario["suppliers"][0]["id"]
    assert order["partTypeId"] == scenario["parts"][0]["id"]
    assert order["quantity"] == quantity
    assert order["syncStatus"] == "PENDING"
    assert order["lingxingPoId"] is None
    assert order["pushedAt"] is None
    assert order["createdAt"] == "2026-07-22T10:30:00+08:00"

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='PO_CREATED' AND object_type='PURCHASE_ORDER'"
        ).fetchone()[0] == 1
    finally:
        database.close()


# Feature: batch-traceability, Property 43: 采购订单创建校验拒绝且无副作用
# Under the free-form model supplier and quantity are optional, so only genuinely
# invalid inputs are rejected: an out-of-range / non-integer quantity when one is
# supplied, or a supplier / 商品 that does not exist.
@pytest.mark.parametrize(
    "payload_patch",
    [
        {"quantity": 0},
        {"quantity": 1000000},
        {"quantity": 1.5},
        {"partTypeId": 999999},
        {"supplierId": 999999},
    ],
)
def test_invalid_purchase_order_is_rejected_without_insert(tmp_path, payload_patch):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    payload = {
        "supplierId": scenario["suppliers"][0]["id"],
        "partTypeId": scenario["parts"][0]["id"],
        "quantity": 1,
    }
    payload.update(payload_patch)
    response = client.post("/api/purchase-orders", json=payload)
    assert response.status_code in {400, 404, 409}
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
    finally:
        database.close()


def test_empty_purchase_order_is_rejected_without_insert(tmp_path):
    """A purchase order with no product, no 商品 and no template fields is rejected."""
    client, database_path, _scenario, _fake = setup_case(tmp_path)
    response = client.post("/api/purchase-orders", json={})
    assert response.status_code == 400
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
    finally:
        database.close()


def test_nonexistent_product_is_rejected_without_insert(tmp_path):
    client, database_path, _scenario, _fake = setup_case(tmp_path)
    response = client.post("/api/purchase-orders", json={"productModelId": 999999})
    assert response.status_code == 404
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
    finally:
        database.close()


def test_free_form_purchase_order_stores_template_fields(tmp_path):
    """Operations may create a product-linked order filled purely from template
    fields, with no supplier / 商品 link, and the fields round-trip back."""
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    response = client.post(
        "/api/purchase-orders",
        json={
            "productModelId": scenario["product"]["id"],
            "fields": {
                "SKU": "SKU-FF-001",
                "供应商": "自填供应商",
                "实际采购量": 120,
                "含税单价": "12.50",
                "店铺": "旗舰店",
            },
        },
    )
    assert response.status_code == 201, response.get_json()
    order = response.get_json()["data"]
    assert order["supplierId"] is None
    assert order["partTypeId"] is None
    assert order["productModelId"] == scenario["product"]["id"]
    assert order["fields"]["SKU"] == "SKU-FF-001"
    assert order["fields"]["供应商"] == "自填供应商"
    assert order["fields"]["店铺"] == "旗舰店"
    assert order["syncStatus"] == "PENDING"

    export = client.get(f"/api/purchase-orders/{order['id']}/export")
    assert export.status_code == 200
    assert export.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

