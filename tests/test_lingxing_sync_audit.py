from __future__ import annotations

import json
import sqlite3

import pytest

from test_inbound_receipts import create_order
from test_lingxing_push import create_receipt
from test_purchase_orders import setup_case
from traceability.db import connect_database


# Feature: batch-traceability, Property 54: 同步状态取值不变式
def test_sync_status_is_initialized_and_database_constrained(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    receipt = create_receipt(client, order)
    assert order["syncStatus"] == receipt["syncStatus"] == "PENDING"
    database = connect_database(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "UPDATE purchase_orders SET sync_status='UNKNOWN' WHERE id=?", (order["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "UPDATE inbound_receipts SET sync_status='UNKNOWN' WHERE id=?", (receipt["id"],)
            )
    finally:
        database.close()


# Feature: batch-traceability, Property 55: 推送执行写入完整审计事件
# Feature: batch-traceability, Property 56: 同步状态查询映射
def test_sync_status_endpoint_matches_persisted_push_and_audit(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    receipt = create_receipt(client, order)
    assert client.post(f"/api/purchase-orders/{order['id']}/push").status_code == 200
    assert client.post(f"/api/inbound-receipts/{receipt['id']}/push").status_code == 200

    po_status = client.get(f"/api/purchase-orders/{order['id']}/sync-status").get_json()["data"]
    receipt_status = client.get(f"/api/inbound-receipts/{receipt['id']}/sync-status").get_json()["data"]
    assert po_status == {
        "id": order["id"],
        "syncStatus": "PUSHED",
        # 采购单下单 returns no object id; the submitted 采购单号 is recorded.
        "lingxingId": order["poNo"],
        "pushedAt": "2026-07-22T10:30:00+08:00",
        "pushError": "",
    }
    assert receipt_status["syncStatus"] == "PUSHED"
    assert receipt_status["lingxingId"] == "LX-IN-1"
    assert receipt_status["pushedAt"] == "2026-07-22T10:30:00+08:00"
    database = connect_database(database_path)
    try:
        events = database.execute(
            """
            SELECT event_type, object_type, object_code, occurred_at, payload_json
            FROM audit_events WHERE event_type IN ('PO_PUSHED','INBOUND_PUSHED') ORDER BY id
            """
        ).fetchall()
        assert len(events) == 2
        assert {row["object_type"] for row in events} == {"PURCHASE_ORDER", "INBOUND_RECEIPT"}
        assert all(
            row["object_code"]
            and row["occurred_at"]
            and json.loads(row["payload_json"])["result"] == "PUSHED"
            for row in events
        )
    finally:
        database.close()


# Feature: batch-traceability, Property 57: 查询不存在记录即拒绝
def test_sync_status_rejects_unknown_identifiers(tmp_path):
    client, _database_path, _scenario, _fake = setup_case(tmp_path)
    assert client.get("/api/purchase-orders/999999/sync-status").status_code == 404
    assert client.get("/api/inbound-receipts/999999/sync-status").status_code == 404


# Feature: batch-traceability, Property 58: 审计事件只追加不可篡改
def test_lingxing_audit_history_only_grows_across_push_attempts(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    first = create_order(client, scenario)
    assert client.post(f"/api/purchase-orders/{first['id']}/push").status_code == 200
    database = connect_database(database_path)
    try:
        before = [tuple(row) for row in database.execute(
            "SELECT event_id, event_type, object_type, object_code, occurred_at, payload_json FROM audit_events WHERE event_type='PO_PUSHED' ORDER BY id"
        )]
    finally:
        database.close()

    second = create_order(client, scenario)
    fake.failures.add("purchase_order")
    assert client.post(f"/api/purchase-orders/{second['id']}/push").status_code == 502
    database = connect_database(database_path)
    try:
        after_success = [tuple(row) for row in database.execute(
            "SELECT event_id, event_type, object_type, object_code, occurred_at, payload_json FROM audit_events WHERE event_type='PO_PUSHED' ORDER BY id"
        )]
        failed_count = database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='PO_PUSH_FAILED'"
        ).fetchone()[0]
        assert after_success == before
        assert failed_count == 1
    finally:
        database.close()
