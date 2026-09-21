"""Flow tests for the 扫码枪入库 quality gate.

The warehouse flow is 生产订单(溯源码) → 批次登记 → 批次质量处理 → 扫码枪入库.
Before this gate the quality step had no effect on stock: a 暂扣 (HOLD) batch
could still be scanned into finished-goods stock. These tests pin the rules:

* a HOLD batch is always refused (no stock, no scan record);
* with 质量放行 enabled, a batch must be registered and released (PASSED) first;
* the lookup carries the flow state so the operator sees it before scanning.
"""

from __future__ import annotations

from test_scan_gun_inbound import setup_production
from traceability.db import connect_database


def register_batch(client, production):
    response = client.post(
        "/api/batch-entry/scan", json={"code": production["identificationCode"]}
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def stock_in(client, production, quantity=3):
    return client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": quantity},
    )


def assert_no_stock(database_path):
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM product_stock").fetchone()[0] == 0
    finally:
        database.close()


def test_held_batch_is_refused_stock_in_without_side_effects(tmp_path):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    record = register_batch(client, production)
    held = client.post(
        f"/api/batch-trace-records/{record['id']}/hold", json={"reason": "外观待复核"}
    )
    assert held.status_code == 200, held.get_json()

    blocked = stock_in(client, production)
    assert blocked.status_code == 409
    assert "暂扣" in blocked.get_json()["message"]
    assert_no_stock(database_path)

    # The lookup explains why, so the operator sees it before scanning.
    lookup = client.post(
        "/api/scan-gun/lookup", json={"code": production["productionQrCode"]}
    )
    assert lookup.status_code == 200, lookup.get_json()
    progress = lookup.get_json()["data"]["progress"]
    assert progress["qualityStatus"] == "HOLD"
    assert progress["canStockIn"] is False
    assert "暂扣" in progress["stockInBlockedReason"]


def test_quality_release_gate_requires_registration_and_pass(tmp_path):
    client, database_path, scenario, _fake, _order, production = setup_production(tmp_path)
    enabled = client.put("/api/settings", json={"requireQualityRelease": True})
    assert enabled.status_code == 200, enabled.get_json()

    # Unregistered batch: refused while the gate is on.
    blocked = stock_in(client, production)
    assert blocked.status_code == 409
    assert "批次登记" in blocked.get_json()["message"]
    assert_no_stock(database_path)

    # Registered but still 待检: still refused.
    record = register_batch(client, production)
    pending = stock_in(client, production)
    assert pending.status_code == 409
    assert "放行" in pending.get_json()["message"]
    assert_no_stock(database_path)

    # Released: the stock-in now succeeds.
    released = client.post(f"/api/batch-trace-records/{record['id']}/pass")
    assert released.status_code == 200, released.get_json()
    accepted = stock_in(client, production, 4)
    assert accepted.status_code == 201, accepted.get_json()
    assert accepted.get_json()["data"]["onHand"] == 4

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT on_hand FROM product_stock WHERE product_model_id = ?",
            (scenario["product"]["id"],),
        ).fetchone()[0] == 4
    finally:
        database.close()


def test_stock_in_stays_open_when_quality_release_is_off(tmp_path):
    """The gate is opt-in: with it off an unregistered batch still receives."""
    client, _database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    accepted = stock_in(client, production, 5)
    assert accepted.status_code == 201, accepted.get_json()
    assert accepted.get_json()["data"]["onHand"] == 5


def test_production_order_progress_tracks_received_and_remaining(tmp_path):
    client, _database_path, _scenario, _fake, _order, production = setup_production(
        tmp_path, quantity=10
    )
    assert stock_in(client, production, 4).status_code == 201

    listed = client.get("/api/production-orders").get_json()["data"]
    progress = next(item["progress"] for item in listed if item["id"] == production["id"])
    assert progress["receivedQuantity"] == 4
    assert progress["remainingQuantity"] == 6
    assert progress["fullyReceived"] is False
    assert progress["registered"] is False
    assert progress["canStockIn"] is True

    assert stock_in(client, production, 6).status_code == 201
    listed = client.get("/api/production-orders").get_json()["data"]
    progress = next(item["progress"] for item in listed if item["id"] == production["id"])
    assert progress["receivedQuantity"] == 10
    assert progress["remainingQuantity"] == 0
    assert progress["fullyReceived"] is True
