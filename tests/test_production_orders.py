from __future__ import annotations

import app as app_module

from test_inbound_receipts import create_order
from test_purchase_orders import setup_case
from traceability.codes import parse_batch_payload
from traceability.db import connect_database


def create_production(client, order):
    response = client.post(
        "/api/production-orders", json={"purchaseOrderId": order["id"]}
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# Feature: batch-traceability, Property 64: 生产订单创建事务性与唯一生产二维码
def test_production_order_creates_one_batch_and_one_unique_qr(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    first_order = create_order(client, scenario, 8)
    second_order = create_order(client, scenario, 9)
    first = create_production(client, first_order)
    second = create_production(client, second_order)

    assert first["productionQrCode"] != second["productionQrCode"]
    assert parse_batch_payload(first["identificationCode"]) == first["productionQrCode"]
    assert first["purchaseOrderId"] == first_order["id"]
    assert first["products"] == [
        {
            "productModelId": scenario["product"]["id"],
            "modelCode": first["productModelCode"],
            "name": scenario["product"]["name"],
            "quantity": 8,
        }
    ]
    qr = client.get(first["downloadUrl"])
    assert qr.status_code == 200
    assert qr.mimetype == "image/svg+xml"

    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 2
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 2
        assert database.execute(
            "SELECT COUNT(DISTINCT batch_code) FROM production_batches"
        ).fetchone()[0] == 2
    finally:
        database.close()


def test_production_order_transaction_rolls_back_orphan_batch_on_failure(
    tmp_path, monkeypatch
):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    original = app_module.record_audit_event

    def fail_on_production(database, event_type, *args, **kwargs):
        if event_type == "PRODUCTION_ORDER_CREATED":
            raise RuntimeError("injected production failure")
        return original(database, event_type, *args, **kwargs)

    monkeypatch.setattr(app_module, "record_audit_event", fail_on_production)
    response = client.post(
        "/api/production-orders", json={"purchaseOrderId": order["id"]}
    )
    assert response.status_code == 500
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 0
    finally:
        database.close()


# Feature: batch-traceability, Property 65: 采购订单—生产订单幂等（一对至多一）
def test_purchase_order_can_generate_at_most_one_production_order(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    first = create_production(client, order)
    response = client.post(
        "/api/production-orders", json={"purchaseOrderId": order["id"]}
    )
    assert response.status_code == 409
    assert "已生成生产订单" in response.get_json()["message"]
    existing = client.get(f"/api/production-orders/{first['id']}").get_json()["data"]
    assert existing["productionQrCode"] == first["productionQrCode"]
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 1
    finally:
        database.close()


# Feature: batch-traceability, Property 66: 生产订单创建校验拒绝且无副作用
def test_missing_or_unknown_purchase_order_creates_nothing(tmp_path):
    client, database_path, _scenario, _fake = setup_case(tmp_path)
    assert client.post("/api/production-orders", json={}).status_code == 400
    assert client.post(
        "/api/production-orders", json={"purchaseOrderId": 999999}
    ).status_code == 404
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 0
    finally:
        database.close()


# Feature: batch-traceability, Property 67: 生产订单来源关联与单产品单行展示
def test_production_detail_preserves_source_and_one_product_per_row(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 12)
    production = create_production(client, order)
    detail = client.get(f"/api/production-orders/{production['id']}")
    assert detail.status_code == 200
    data = detail.get_json()["data"]
    assert data["purchaseOrderId"] == order["id"]
    assert len(data["products"]) == 1
    assert data["products"][0]["productModelId"] == scenario["product"]["id"]
    assert data["products"][0]["quantity"] == 12
