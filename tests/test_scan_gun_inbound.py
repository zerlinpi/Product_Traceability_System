from __future__ import annotations

import pytest

import app as app_module
from test_inbound_receipts import create_order
from test_production_orders import create_production
from test_purchase_orders import setup_case
from traceability.db import connect_database


def setup_production(tmp_path, quantity=10):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario, quantity)
    production = create_production(client, order)
    return client, database_path, scenario, fake, order, production


# Feature: batch-traceability, Property 68: 扫码枪按生产二维码检索生产订单
def test_scan_gun_lookup_resolves_payload_and_bare_batch_code(tmp_path):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    for code in (production["identificationCode"], production["productionQrCode"]):
        response = client.post("/api/scan-gun/lookup", json={"code": code})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["data"]["id"] == production["id"]

    before = connect_database(database_path)
    try:
        counts = (
            before.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0],
            before.execute("SELECT COUNT(*) FROM product_stock").fetchone()[0],
        )
    finally:
        before.close()
    invalid = client.post("/api/scan-gun/lookup", json={"code": "PTS:B:NOT-FOUND"})
    assert invalid.status_code == 404
    database = connect_database(database_path)
    try:
        assert (
            database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0],
            database.execute("SELECT COUNT(*) FROM product_stock").fetchone()[0],
        ) == counts
    finally:
        database.close()


# Feature: batch-traceability, Property 69: 扫码枪入库累加库存的原子性与最新库存返回
def test_scan_gun_inbound_accumulates_and_returns_latest_stock(tmp_path):
    client, database_path, scenario, _fake, _order, production = setup_production(tmp_path)
    latest = 0
    for quantity in (1, 7, 20):
        latest += quantity
        response = client.post(
            "/api/scan-gun/inbound",
            json={"productionOrderId": production["id"], "quantity": quantity},
        )
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["data"]["onHand"] == latest

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT COUNT(*) FROM inbound_scan_records WHERE production_order_id=?",
            (production["id"],),
        ).fetchone()[0] == 3
        assert database.execute(
            "SELECT on_hand FROM product_stock WHERE product_model_id=?",
            (scenario["product"]["id"],),
        ).fetchone()[0] == latest
    finally:
        database.close()


def test_scan_gun_inbound_rolls_back_record_and_stock_together(tmp_path, monkeypatch):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    original = app_module.record_audit_event

    def fail_on_inbound(database, event_type, *args, **kwargs):
        if event_type == "FINISHED_GOODS_RECEIVED":
            raise RuntimeError("injected inbound failure")
        return original(database, event_type, *args, **kwargs)

    monkeypatch.setattr(app_module, "record_audit_event", fail_on_inbound)
    response = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 5},
    )
    assert response.status_code == 500
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM product_stock").fetchone()[0] == 0
    finally:
        database.close()


# Feature: batch-traceability, Property 70: 扫码枪入库数量校验拒绝且无副作用
@pytest.mark.parametrize("quantity", [None, "", 0, -1, 1000000, 1.5, True])
def test_invalid_scan_gun_quantity_has_no_side_effect(tmp_path, quantity):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    response = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": quantity},
    )
    assert response.status_code == 400
    assert "1-999999" in response.get_json()["message"]
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM product_stock").fetchone()[0] == 0
    finally:
        database.close()
