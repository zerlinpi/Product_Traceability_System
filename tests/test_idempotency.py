"""Idempotency for field-facing write endpoints.

The headline risk: a scan gun or a flaky LAN delivers the same submission twice.
The client times out, the operator retries, and the server has already
committed — so finished-goods stock is credited twice, silently. These tests
pin the fix.

Feature: batch-traceability, Property 70: 现场写操作的幂等重放
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capability_helpers import make_auth_app  # noqa: E402
from test_inbound_receipts import create_order  # noqa: E402
from test_production_orders import create_production  # noqa: E402
from test_purchase_orders import setup_case  # noqa: E402
from traceability import idempotency  # noqa: E402
from traceability.db import connect_database  # noqa: E402

FIXED_NOW = "2026-07-22T10:30:00+08:00"
NOW_EPOCH = int(datetime.fromisoformat(FIXED_NOW).timestamp())


def setup_production(tmp_path, quantity=10):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario, quantity)
    production = create_production(client, order)
    return client, database_path, scenario, fake, order, production


def _stock(database_path) -> int:
    database = connect_database(database_path)
    try:
        row = database.execute("SELECT COALESCE(SUM(on_hand), 0) FROM product_stock").fetchone()
        return int(row[0])
    finally:
        database.close()


def _inbound_count(database_path) -> int:
    database = connect_database(database_path)
    try:
        return int(database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0])
    finally:
        database.close()


# --------------------------------------------------------------------------
# 1. Migration
# --------------------------------------------------------------------------


def test_migration_v20_creates_the_idempotency_table(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 21
        columns = {
            row[1] for row in database.execute("PRAGMA table_info(idempotency_keys)").fetchall()
        }
        assert columns == {
            "idempotency_key",
            "scope",
            "request_fingerprint",
            "state",
            "status_code",
            "response_json",
            "actor_user_id",
            "started_at",
            "completed_at",
        }
    finally:
        database.close()


def test_v20_is_additive_and_keeps_foreign_keys_clean(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        # Pre-existing tables are untouched.
        for table in ("users", "product_models", "inbound_scan_records", "product_stock"):
            assert database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
            ).fetchone()
    finally:
        database.close()


# --------------------------------------------------------------------------
# 2. Module-level behaviour
# --------------------------------------------------------------------------


def test_normalize_key_rejects_short_overlong_and_non_printable():
    with pytest.raises(idempotency.IdempotencyError):
        idempotency.normalize_key(None)
    with pytest.raises(idempotency.IdempotencyError):
        idempotency.normalize_key("short")
    with pytest.raises(idempotency.IdempotencyError):
        idempotency.normalize_key("x" * (idempotency.MAX_KEY_LENGTH + 1))
    with pytest.raises(idempotency.IdempotencyError):
        idempotency.normalize_key("bad\x00key12345")
    assert idempotency.normalize_key("  good-key-12345  ") == "good-key-12345"


def test_fingerprint_is_stable_and_key_order_independent():
    left = idempotency.fingerprint({"a": 1, "b": [1, 2]})
    right = idempotency.fingerprint({"b": [1, 2], "a": 1})
    assert left == right
    assert left != idempotency.fingerprint({"a": 1, "b": [2, 1]})


def _claim(database, key, payload, *, scope="test.scope"):
    return idempotency.claim(
        database,
        key=key,
        scope=scope,
        request_fingerprint=idempotency.fingerprint(payload),
        actor_user_id=None,
        started_at=FIXED_NOW,
    )


def test_claim_then_complete_then_replay(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        outcome, row = _claim(database, "key-0000001", {"quantity": 5})
        assert (outcome, row) == (idempotency.CLAIMED, None)

        idempotency.complete(
            database,
            key="key-0000001",
            scope="test.scope",
            status_code=201,
            body={"ok": True, "data": {"onHand": 5}},
            completed_at=FIXED_NOW,
        )

        outcome, row = _claim(database, "key-0000001", {"quantity": 5})
        assert outcome == idempotency.REPLAY
        assert row["status_code"] == 201
        assert "onHand" in row["response_json"]
    finally:
        database.close()


def test_reusing_a_key_with_a_different_payload_conflicts(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        assert _claim(database, "key-0000002", {"quantity": 5})[0] == idempotency.CLAIMED
        outcome, _row = _claim(database, "key-0000002", {"quantity": 999})
        assert outcome == idempotency.CONFLICT
    finally:
        database.close()


def test_an_unfinished_claim_reports_in_progress(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        assert _claim(database, "key-0000003", {"quantity": 1})[0] == idempotency.CLAIMED
        assert _claim(database, "key-0000003", {"quantity": 1})[0] == idempotency.IN_PROGRESS
    finally:
        database.close()


def test_release_frees_an_in_progress_claim(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        _claim(database, "key-0000004", {"quantity": 1})
        idempotency.release(database, key="key-0000004", scope="test.scope")
        assert _claim(database, "key-0000004", {"quantity": 1})[0] == idempotency.CLAIMED
    finally:
        database.close()


def test_release_never_deletes_a_completed_result(tmp_path):
    """A completed key must survive release, or a replay would re-run the write."""
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        _claim(database, "key-0000005", {"quantity": 1})
        idempotency.complete(
            database,
            key="key-0000005",
            scope="test.scope",
            status_code=200,
            body={"ok": True},
            completed_at=FIXED_NOW,
        )
        idempotency.release(database, key="key-0000005", scope="test.scope")
        assert _claim(database, "key-0000005", {"quantity": 1})[0] == idempotency.REPLAY
    finally:
        database.close()


def test_scope_separates_keys(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        assert _claim(database, "key-0000006", {"q": 1}, scope="a")[0] == idempotency.CLAIMED
        assert _claim(database, "key-0000006", {"q": 1}, scope="b")[0] == idempotency.CLAIMED
    finally:
        database.close()


def test_clear_in_progress_removes_stranded_claims(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        _claim(database, "key-0000007", {"quantity": 1})
        assert idempotency.clear_in_progress(database) == 1
        assert idempotency.lookup(database, "key-0000007", "test.scope") is None
    finally:
        database.close()


def test_purge_expired_respects_the_retention_window(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        _claim(database, "key-0000008", {"quantity": 1})
        idempotency.complete(
            database,
            key="key-0000008",
            scope="test.scope",
            status_code=200,
            body={"ok": True},
            completed_at=FIXED_NOW,
        )
        # Nothing is old enough yet.
        assert idempotency.purge_expired(database, now_epoch=NOW_EPOCH) == 0
        # A day past the retention window, it goes.
        later = NOW_EPOCH + idempotency.RETENTION_SECONDS + 24 * 60 * 60
        assert idempotency.purge_expired(database, now_epoch=later) == 1
    finally:
        database.close()


def test_the_key_column_is_case_sensitive(tmp_path):
    """Keys are opaque: 'ABC' and 'abc' are different keys."""
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        assert _claim(database, "Key-Case-0001", {"q": 1})[0] == idempotency.CLAIMED
        assert _claim(database, "key-case-0001", {"q": 1})[0] == idempotency.CLAIMED
    finally:
        database.close()


# --------------------------------------------------------------------------
# 3. End-to-end: the scan-gun double-credit scenario
# --------------------------------------------------------------------------


def test_scan_gun_retry_with_the_same_key_does_not_double_credit_stock(tmp_path):
    """The headline bug. A retried scan-gun submission must not add stock twice."""
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    key = "scan-gun-retry-0001"
    payload = {"productionOrderId": production["id"], "quantity": 7}

    first = client.post("/api/scan-gun/inbound", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.get_json()
    assert _stock(database_path) == 7
    assert _inbound_count(database_path) == 1

    # The client timed out and retried with the same key.
    second = client.post("/api/scan-gun/inbound", json=payload, headers={"Idempotency-Key": key})
    assert second.status_code == first.status_code
    assert second.get_json() == first.get_json(), "the replayed body must be identical"

    assert _stock(database_path) == 7, "stock was credited twice"
    assert _inbound_count(database_path) == 1, "a duplicate inbound record was written"


def test_scan_gun_retry_via_the_body_field_also_replays(tmp_path):
    """The key may travel in the JSON body for clients that cannot set headers."""
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    payload = {
        "productionOrderId": production["id"],
        "quantity": 3,
        "idempotencyKey": "scan-gun-body-0001",
    }
    first = client.post("/api/scan-gun/inbound", json=payload)
    second = client.post("/api/scan-gun/inbound", json=payload)
    assert first.status_code == second.status_code == 201
    assert _stock(database_path) == 3
    assert _inbound_count(database_path) == 1


def test_without_a_key_the_old_accumulating_behaviour_is_unchanged(tmp_path):
    """Idempotency is opt-in: existing clients keep working exactly as before."""
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    payload = {"productionOrderId": production["id"], "quantity": 4}
    for _ in range(3):
        assert client.post("/api/scan-gun/inbound", json=payload).status_code == 201
    assert _stock(database_path) == 12
    assert _inbound_count(database_path) == 3


def test_two_genuine_submissions_with_different_keys_both_apply(tmp_path):
    """Continuous scan-gun work must not be blocked by the idempotency layer."""
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    for index, quantity in enumerate((5, 6), start=1):
        response = client.post(
            "/api/scan-gun/inbound",
            json={"productionOrderId": production["id"], "quantity": quantity},
            headers={"Idempotency-Key": f"scan-gun-distinct-{index:04d}"},
        )
        assert response.status_code == 201, response.get_json()
    assert _stock(database_path) == 11
    assert _inbound_count(database_path) == 2


def test_reusing_a_key_with_a_different_quantity_is_rejected(tmp_path):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    key = "scan-gun-conflict-0001"
    assert (
        client.post(
            "/api/scan-gun/inbound",
            json={"productionOrderId": production["id"], "quantity": 5},
            headers={"Idempotency-Key": key},
        ).status_code
        == 201
    )
    conflict = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 50},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert _stock(database_path) == 5, "the conflicting request must not have applied"


def test_a_duplicate_while_the_original_is_in_flight_is_refused(tmp_path):
    """Claim-then-complete: the second request is refused, never executed twice."""
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    key = "scan-gun-inflight-0001"
    payload = {"productionOrderId": production["id"], "quantity": 9}

    # Simulate a process holding the claim mid-request.
    database = connect_database(database_path)
    try:
        idempotency.claim(
            database,
            key=key,
            scope="scan-gun.inbound",
            request_fingerprint=idempotency.fingerprint(payload),
            actor_user_id=None,
            started_at=FIXED_NOW,
        )
    finally:
        database.close()

    blocked = client.post("/api/scan-gun/inbound", json=payload, headers={"Idempotency-Key": key})
    assert blocked.status_code == 409
    assert _stock(database_path) == 0
    assert _inbound_count(database_path) == 0


def test_a_rejected_request_frees_the_key_so_the_client_can_retry(tmp_path):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    key = "scan-gun-retry-after-error-01"

    bad = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 0},
        headers={"Idempotency-Key": key},
    )
    assert bad.status_code == 400, bad.get_json()
    assert _stock(database_path) == 0

    good = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 2},
        headers={"Idempotency-Key": key},
    )
    assert good.status_code == 201, good.get_json()
    assert _stock(database_path) == 2


def test_an_unusable_key_is_rejected_with_400(tmp_path):
    client, _database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    response = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 1},
        headers={"Idempotency-Key": "tiny"},
    )
    assert response.status_code == 400
    assert "幂等键" in response.get_json()["message"]


def test_a_stranded_claim_is_cleared_on_restart(tmp_path):
    """A crash mid-request must not block that key forever."""
    app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        idempotency.claim(
            database,
            key="stranded-key-0001",
            scope="scan-gun.inbound",
            request_fingerprint="x",
            actor_user_id=None,
            started_at=FIXED_NOW,
        )
    finally:
        database.close()

    # Re-initialising the app runs _clear_abandoned_idempotency_claims.
    from app import create_app

    create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "SECRET_KEY": "idempotency-restart",
            "NOW_PROVIDER": lambda: FIXED_NOW,
        }
    )
    database = connect_database(database_path)
    try:
        assert idempotency.lookup(database, "stranded-key-0001", "scan-gun.inbound") is None
    finally:
        database.close()


def test_replay_survives_a_database_reconnect(tmp_path):
    """The stored result is durable, not in-process memoisation."""
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    key = "scan-gun-durable-0001"
    payload = {"productionOrderId": production["id"], "quantity": 8}
    first = client.post("/api/scan-gun/inbound", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 201

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT state, status_code, response_json FROM idempotency_keys "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        assert row["state"] == "completed"
        assert row["status_code"] == 201
        assert row["response_json"]
    finally:
        database.close()

    second = client.post("/api/scan-gun/inbound", json=payload, headers={"Idempotency-Key": key})
    assert second.get_json() == first.get_json()
    assert _stock(database_path) == 8


def test_idempotency_keys_are_not_written_when_the_request_has_no_key(tmp_path):
    client, database_path, _scenario, _fake, _order, production = setup_production(tmp_path)
    client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 1},
    )
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        database.close()


def test_the_unique_constraint_is_on_key_and_scope_together(tmp_path):
    """Guards against a future migration dropping one half of the primary key."""
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = connect_database(database_path)
    try:
        database.execute(
            "INSERT INTO idempotency_keys(idempotency_key, scope, request_fingerprint, "
            "state, started_at) VALUES ('dup-key-0001', 'a', 'f', 'in_progress', ?)",
            (FIXED_NOW,),
        )
        database.commit()
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO idempotency_keys(idempotency_key, scope, request_fingerprint, "
                "state, started_at) VALUES ('dup-key-0001', 'a', 'f', 'in_progress', ?)",
                (FIXED_NOW,),
            )
    finally:
        database.close()
