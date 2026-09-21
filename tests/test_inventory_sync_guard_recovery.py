"""A crashed 库存同步 must not brick the feature forever.

The sync sets ``inventory_sync.in_progress`` before calling Lingxing and clears it
when it finishes. If the process dies in between (restart / kill / power loss) the
flag used to stay at "1" permanently, so every later sync answered 409
"库存同步正在进行中" and only manual DB surgery could recover it. A guard older than
the timeout is now treated as abandoned and taken over.
"""

from __future__ import annotations

from test_inbound_receipts import create_order
from test_inventory_sync import inbound_stock
from test_production_orders import create_production
from test_purchase_orders import setup_case
from traceability.db import connect_database


def strand_guard(database_path, updated_at):
    """Simulate a sync that died after taking the guard."""
    database = connect_database(database_path)
    try:
        database.execute(
            """
            INSERT INTO app_settings(setting_key, setting_value, is_secret,
                                     updated_by, updated_by_user_id, updated_at)
            VALUES ('inventory_sync.in_progress', '1', 0, 'tester', NULL, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = '1', updated_at = excluded.updated_at
            """,
            (updated_at,),
        )
        database.commit()
    finally:
        database.close()


def guard_value(database_path):
    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT setting_value FROM app_settings "
            "WHERE setting_key = 'inventory_sync.in_progress'"
        ).fetchone()
        return row["setting_value"] if row else None
    finally:
        database.close()


def stocked(client, scenario, fake, quantity=6, order_sn="CR-GUARD"):
    order = create_order(client, scenario, quantity)
    production = create_production(client, order)
    inbound_stock(client, production, quantity)
    fake.add_receipt(order_sn, production["productModelCode"], quantity)
    return production


def test_fresh_guard_still_blocks_a_concurrent_sync(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    stocked(client, scenario, fake)
    # The frozen clock is 2026-07-22T10:30:00+08:00, so this guard is 1 minute old.
    strand_guard(database_path, "2026-07-22T10:29:00+08:00")
    blocked = client.post("/api/inventory-sync")
    assert blocked.status_code == 409
    assert "正在进行中" in blocked.get_json()["message"]
    assert guard_value(database_path) == "1"
    assert fake.calls == []


def test_stale_guard_is_taken_over_and_audited(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    stocked(client, scenario, fake)
    # Well past the 15 minute window: the previous run clearly died.
    strand_guard(database_path, "2026-07-22T09:00:00+08:00")

    recovered = client.post("/api/inventory-sync")
    assert recovered.status_code == 200, recovered.get_json()
    assert recovered.get_json()["data"]["syncStatus"] == "PUSHED"
    # The guard was released again, so the feature stays usable.
    assert guard_value(database_path) == "0"
    assert [op for op, _ in fake.calls] == ["receipt_list", "inventory_sync"]

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type = 'INVENTORY_SYNC_GUARD_RECOVERED'"
        ).fetchone()[0] == 1
    finally:
        database.close()


def test_guard_without_a_usable_start_time_keeps_blocking(tmp_path):
    """Concurrency protection wins when we cannot prove the guard was abandoned."""
    client, database_path, scenario, fake = setup_case(tmp_path)
    stocked(client, scenario, fake)
    strand_guard(database_path, "not-a-timestamp")
    blocked = client.post("/api/inventory-sync")
    assert blocked.status_code == 409
    assert fake.calls == []


def test_startup_clears_guards_left_by_a_dead_process(tmp_path):
    """A crash cannot strand the guard: startup releases it deterministically.

    No request can be in flight while the database initializes, so a flag that is
    still set belongs to a process that never finished.
    """
    from app import create_app

    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario, 6)
    production = create_production(client, order)
    inbound_stock(client, production, 6)
    fake.add_receipt("CR-RESTART", production["productModelCode"], 6)

    # Strand both the sync guard and the per-order push guard, with a *recent*
    # timestamp so only a restart (not the timeout) can clear them.
    strand_guard(database_path, "2026-07-22T10:29:50+08:00")
    strand_push_guard(database_path, "purchase_orders", order["id"], "2026-07-22T10:29:50+08:00")

    # Restarting the app re-runs initialize_database, which releases the guards.
    create_app({"DATABASE": str(database_path), "TESTING": True})

    assert guard_value(database_path) == "0"
    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT push_in_progress FROM purchase_orders WHERE id = ?", (order["id"],)
        ).fetchone()[0] == 0
    finally:
        database.close()

    # And the feature works again straight away.
    assert client.post("/api/inventory-sync").status_code == 200


# ---------------------------------------------------------------------------
# The same hazard for the per-record 领星 push guards (采购单 / 供应收货): a crash
# between taking ``push_in_progress`` and clearing it must not block that single
# record from ever being pushed again.
# ---------------------------------------------------------------------------


def strand_push_guard(database_path, table, row_id, started_at):
    database = connect_database(database_path)
    try:
        database.execute(
            f"UPDATE {table} SET push_in_progress = 1, push_started_at = ? WHERE id = ?",
            (started_at, row_id),
        )
        database.commit()
    finally:
        database.close()


def test_fresh_push_guard_still_blocks_a_concurrent_push(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario, 4)
    strand_push_guard(database_path, "purchase_orders", order["id"], "2026-07-22T10:29:30+08:00")
    blocked = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert blocked.status_code == 409
    assert "正在进行中" in blocked.get_json()["message"]
    assert fake.calls == []


def test_stale_push_guard_is_recovered(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario, 4)
    # The previous attempt died over an hour ago.
    strand_push_guard(database_path, "purchase_orders", order["id"], "2026-07-22T09:00:00+08:00")

    pushed = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert pushed.status_code == 200, pushed.get_json()
    assert pushed.get_json()["data"]["syncStatus"] == "PUSHED"
    assert [op for op, _ in fake.calls] == ["purchase_order"]

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT push_in_progress, sync_status FROM purchase_orders WHERE id = ?",
            (order["id"],),
        ).fetchone()
        assert row["push_in_progress"] == 0
        assert row["sync_status"] == "PUSHED"
    finally:
        database.close()
