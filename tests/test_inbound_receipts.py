from __future__ import annotations

import pytest

from test_purchase_orders import setup_case
from traceability.db import connect_database


def create_order(client, scenario, quantity=10):
    response = client.post(
        "/api/purchase-orders",
        json={
            "supplierId": scenario["suppliers"][0]["id"],
            "partTypeId": scenario["parts"][0]["id"],
            "quantity": quantity,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# Feature: batch-traceability, Property 44: 入库收货创建保真且初始化为待推送
@pytest.mark.parametrize("quantity", [1, 30, 999999])
def test_inbound_receipt_is_faithful_and_pending(tmp_path, quantity):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    response = client.post(
        "/api/inbound-receipts",
        json={"purchaseOrderId": order["id"], "quantity": quantity},
    )
    assert response.status_code == 201, response.get_json()
    receipt = response.get_json()["data"]
    assert receipt["purchaseOrderId"] == order["id"]
    assert receipt["quantity"] == quantity
    assert receipt["syncStatus"] == "PENDING"
    assert receipt["lingxingInboundId"] is None
    assert receipt["pushedAt"] is None
    assert receipt["receivedAt"] == "2026-07-22T10:30:00+08:00"

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='INBOUND_RECEIVED'"
        ).fetchone()[0] == 1
    finally:
        database.close()


# Feature: batch-traceability, Property 45: 入库收货创建校验拒绝且无副作用
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"purchaseOrderId": 999999, "quantity": 1},
        {"purchaseOrderId": 1, "quantity": ""},
        {"purchaseOrderId": 1, "quantity": 0},
        {"purchaseOrderId": 1, "quantity": 1000000},
        {"purchaseOrderId": 1, "quantity": 1.25},
    ],
)
def test_invalid_inbound_receipt_has_no_side_effect(tmp_path, payload):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    if payload.get("purchaseOrderId") == 1:
        payload = {**payload, "purchaseOrderId": order["id"]}
    response = client.post("/api/inbound-receipts", json=payload)
    assert response.status_code in {400, 404}
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM inbound_receipts").fetchone()[0] == 0
    finally:
        database.close()

