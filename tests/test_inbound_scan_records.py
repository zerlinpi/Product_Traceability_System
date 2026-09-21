"""成品扫码入库记录 listing.

``POST /api/scan-gun/inbound`` writes ``inbound_scan_records``, but nothing used
to read that table, so an operator saw no record after scanning. These tests pin
the listing endpoint that the 扫码枪入库 page renders.
"""
from __future__ import annotations

from capability_helpers import bootstrap_admin, insert_user, login, make_auth_app
from test_inbound_receipts import create_order
from test_production_orders import create_production
from test_purchase_orders import setup_case


def receive(client, production, quantity):
    response = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": quantity},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def setup_production(tmp_path, quantity=10):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, quantity)
    return client, database_path, scenario, order, create_production(client, order)


def test_scan_inbound_appears_in_the_record_list(tmp_path):
    client, _database_path, scenario, order, production = setup_production(tmp_path)
    assert client.get("/api/inbound-scan-records").get_json()["data"] == []

    receive(client, production, 4)
    receive(client, production, 6)

    records = client.get("/api/inbound-scan-records").get_json()["data"]
    assert len(records) == 2
    latest = records[0]
    # Newest first, and the running stock after that scan is reported.
    assert latest["quantity"] == 6
    assert latest["onHand"] == 10
    assert latest["productionOrderId"] == production["id"]
    assert latest["productionQrCode"] == production["productionQrCode"]
    assert latest["poNo"] == order["poNo"]
    assert latest["productModelId"] == scenario["product"]["id"]
    assert latest["productName"] == scenario["product"]["name"]
    assert latest["productModelCode"] == production["productModelCode"]
    assert latest["receivedAt"]
    assert set(records[0]) == {
        "id", "productionOrderId", "productionQrCode", "poNo", "productModelId",
        "productModelCode", "productName", "quantity", "operatorName",
        "operatorUserId", "receivedAt", "onHand",
    }


def test_records_can_be_filtered_by_production_order_and_limited(tmp_path):
    client, _database_path, scenario, _order, production = setup_production(tmp_path)
    second_order = create_order(client, scenario, 5)
    second_production = create_production(client, second_order)
    receive(client, production, 1)
    receive(client, second_production, 2)
    receive(client, production, 3)

    scoped = client.get(
        f"/api/inbound-scan-records?productionOrderId={production['id']}"
    ).get_json()["data"]
    assert [item["quantity"] for item in scoped] == [3, 1]
    assert {item["productionOrderId"] for item in scoped} == {production["id"]}

    limited = client.get("/api/inbound-scan-records?limit=1").get_json()["data"]
    assert len(limited) == 1
    # An out-of-range limit is clamped rather than rejected.
    assert len(client.get("/api/inbound-scan-records?limit=9999").get_json()["data"]) == 3
    assert client.get("/api/inbound-scan-records?limit=abc").status_code == 400


def authenticated_production(tmp_path):
    """An auth-enabled app with a product, purchase order and production order."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)

    def created(path, body):
        response = admin.post(path, json=body, headers={"X-CSRF-Token": csrf})
        assert response.status_code in (200, 201), (path, response.get_json())
        return response.get_json()["data"]

    product = created("/api/products", {"name": "完整成品", "components": []})
    order = created(
        "/api/purchase-orders",
        {"productModelId": product["id"], "fields": {"实际采购量": 8}},
    )
    production = created("/api/production-orders", {"purchaseOrderId": order["id"]})
    return app, database_path, admin, product, production


def test_record_list_records_the_scanning_operator(tmp_path):
    app, database_path, _admin, _product, production = authenticated_production(tmp_path)
    insert_user(database_path, "warehouse.bob", "WAREHOUSE")
    bob, bob_csrf = login(app, "warehouse.bob")
    response = bob.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 2},
        headers={"X-CSRF-Token": bob_csrf},
    )
    assert response.status_code == 201, response.get_json()
    record = bob.get("/api/inbound-scan-records").get_json()["data"][0]
    assert record["operatorName"] == "warehouse.bob"
    assert record["operatorUserId"] is not None
    assert record["quantity"] == 2


def test_record_list_requires_warehouse_or_admin(tmp_path):
    app, database_path, admin, _product, _production = authenticated_production(tmp_path)
    insert_user(database_path, "ops.carol", "OPERATIONS")
    carol, _csrf = login(app, "ops.carol")
    # 成品入库 is a warehouse activity; operations have no access.
    assert carol.get("/api/inbound-scan-records").status_code == 403
    assert app.test_client().get("/api/inbound-scan-records").status_code == 401
    assert admin.get("/api/inbound-scan-records").status_code == 200
