"""A timed-out inventory sync is 结果未知, and the database now says so.

The inventory sync fast-receives every pending 收货单 for a product's SKU. A
timeout on that call used to be recorded as FAILED — and the only action the
operations screen offers after a failure is to sync again. Whether that is safe
depends on Lingxing having already made the 收货单 non-pending, which is exactly
what the lost reply would have told us.

v24 gives that outcome its own value, the same way v23 did for the two push
tables. These tests hold down both halves: the migration, and the sync path
writing it. The business scaffolding is the one test_inventory_sync already
uses — a real product, a real production order, stock received through the scan
gun — because a stock sync that has no stock to sync returns 404 before it ever
reaches Lingxing, and a test that never gets there proves nothing.

Feature: batch-traceability, Property 78: 库存同步结果未知
"""

from __future__ import annotations

import sqlite3

from pathlib import Path

from app import create_app
from batch_strategies import build_product_scenario
from capability_helpers import FakeLingxingService
from test_inbound_receipts import create_order
from test_inventory_sync import inbound_stock
from test_production_orders import create_production
from traceability.db import SCHEMA_VERSION, connect_database

ROOT = Path(__file__).resolve().parent.parent


def setup_case(tmp_path, *, uncertain=(), failures=()):
    """A stocked product whose Lingxing transport answers as the test asks."""
    database_path = tmp_path / "stock-sync.db"
    fake = FakeLingxingService(failures=set(failures), uncertain=set(uncertain))
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "NOW_PROVIDER": lambda: "2026-07-22T10:30:00+08:00",
            "LINGXING_SERVICE_FACTORY": lambda _database: fake,
        }
    )
    client = app.test_client()
    scenario = build_product_scenario(client, "STK", [1], 100)
    order = create_order(client, scenario, 6)
    production = create_production(client, order)
    inbound_stock(client, production, 6)
    # One pending 收货单 for this product's SKU, so the sync has something to
    # fast-receive and the failure being tested is the transport's, not a 404.
    fake.add_receipt("CR-TIMEOUT-1", production["productModelCode"], 6)
    return client, database_path, scenario["product"]["id"], fake


def _status_of(database_path, product_model_id):
    connection = connect_database(database_path)
    try:
        row = connection.execute(
            "SELECT sync_status, push_error FROM product_stock_sync WHERE product_model_id = ?",
            (product_model_id,),
        ).fetchone()
        return None if row is None else (row["sync_status"], row["push_error"])
    finally:
        connection.close()


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------


def _downgrade_product_stock_sync(connection: sqlite3.Connection) -> None:
    """Rebuild the table to its pre-v24 three-value shape, and prove it is that.

    Copied rather than imported: the point is a table that predates v24, and the
    old definition is the only honest way to get one.
    """
    connection.execute("DROP TABLE IF EXISTS product_stock_sync_v24")
    connection.execute(
        """
        CREATE TABLE product_stock_sync_v24 (
            product_model_id INTEGER PRIMARY KEY
                REFERENCES product_models(id) ON DELETE CASCADE,
            sync_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
            synced_quantity INTEGER NOT NULL DEFAULT 0,
            lingxing_id TEXT NOT NULL DEFAULT '',
            push_error TEXT NOT NULL DEFAULT '',
            synced_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO product_stock_sync_v24(
            product_model_id, sync_status, synced_quantity, lingxing_id,
            push_error, synced_at
        )
        SELECT product_model_id, sync_status, synced_quantity, lingxing_id,
               push_error, synced_at
        FROM product_stock_sync
        """
    )
    connection.execute("DROP TABLE product_stock_sync")
    connection.execute("ALTER TABLE product_stock_sync_v24 RENAME TO product_stock_sync")
    connection.execute("PRAGMA user_version = 23")
    connection.commit()


def test_an_old_database_upgrades_without_losing_its_rows(tmp_path):
    database_path = tmp_path / "old.db"
    create_app({"TESTING": True, "DATABASE": str(database_path)})

    connection = connect_database(database_path)
    try:
        # Real product_models rows: the table carries a live foreign key, so a
        # synthetic id would be rejected, and one sneaked past enforcement would
        # then be flagged by the upgrade's own foreign_key_check. The family is
        # seeded the way initialize_database seeds TREADMILL — a bare family row
        # is enough, because what the rebuild has to preserve is the sync row, not
        # the catalogue around it.
        connection.execute(
            "INSERT INTO product_families(product_code, name, active, created_at, updated_at) "
            "VALUES ('STK', '库存型号', 1, 'now', 'now')"
        )
        family_id = connection.execute(
            "SELECT id FROM product_families WHERE product_code = 'STK'"
        ).fetchone()[0]
        for index in range(1, 4):
            connection.execute(
                "INSERT INTO product_models(product_family_id, model_code, name, created_at, updated_at)"
                " VALUES (?, ?, ?, 'now', 'now')",
                (family_id, f"OLD-{index}", f"型号{index}"),
            )
        # One row per old status, so the copy is verified rather than assumed.
        for index, status in enumerate(("PENDING", "PUSHED", "FAILED"), start=1):
            connection.execute(
                "INSERT INTO product_stock_sync(product_model_id, sync_status, synced_quantity)"
                " VALUES (?, ?, ?)",
                (index, status, index * 10),
            )
        connection.commit()
        _downgrade_product_stock_sync(connection)
        assert "NEEDS_REVIEW" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='product_stock_sync'"
        ).fetchone()[0], "the fixture must really be the old three-value shape"
    finally:
        connection.close()

    create_app({"TESTING": True, "DATABASE": str(database_path)})

    connection = connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 24, "v24 is the migration under test"
        rows = connection.execute(
            "SELECT product_model_id, sync_status, synced_quantity "
            "FROM product_stock_sync ORDER BY product_model_id"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in rows] == [
            (1, "PENDING", 10),
            (2, "PUSHED", 20),
            (3, "FAILED", 30),
        ], "every pre-v24 row must survive the rebuild unchanged"
    finally:
        connection.close()


def test_a_fresh_database_refuses_an_invented_status(tmp_path):
    database_path = tmp_path / "fresh.db"
    create_app({"TESTING": True, "DATABASE": str(database_path)})
    connection = connect_database(database_path)
    raised = False
    try:
        connection.execute(
            "INSERT INTO product_models(model_code, name) VALUES ('M-1', '型号')"
        )
        connection.execute(
            "INSERT INTO product_stock_sync(product_model_id, sync_status) VALUES (1, 'NOT_A_STATUS')"
        )
    except sqlite3.IntegrityError:
        raised = True
    finally:
        connection.close()
    assert raised, "the CHECK must reject a value nobody defined"


# --------------------------------------------------------------------------
# The sync path
# --------------------------------------------------------------------------


def test_a_timed_out_fast_receive_is_marked_needs_review(tmp_path):
    """The outcome that matters: 快捷入库 timed out, so the status says look first.

    The message has to say what to do, not just that something went wrong — a
    FAILED badge next to a sync button is how a duplicate 收货单 happens.
    """
    client, database_path, product_model_id, _fake = setup_case(
        tmp_path, uncertain={"inventory_sync"}
    )

    response = client.post("/api/inventory-sync")
    # Every scoped product is NEEDS_REVIEW, so the endpoint reports the failure the
    # same way it always has — but the message it reports is the one that matters.
    assert response.status_code == 502, response.get_json()
    assert "核对该收货单是否已快捷入库" in response.get_json()["message"]

    status, error = _status_of(database_path, product_model_id)
    assert status == "NEEDS_REVIEW"
    assert "核对该收货单是否已快捷入库" in error


def test_a_definite_rejection_is_still_marked_failed(tmp_path):
    """Not every failure is ambiguous, and flattening them would lose the signal."""
    client, database_path, product_model_id, _fake = setup_case(
        tmp_path, failures={"inventory_sync"}
    )

    response = client.post("/api/inventory-sync")
    assert response.status_code == 502, response.get_json()

    status, _error = _status_of(database_path, product_model_id)
    assert status == "FAILED"


def test_a_timed_out_receipt_query_marks_every_product_needs_review(tmp_path):
    """The query itself is unknown, so nothing about any product is known.

    This path used to write FAILED for every product in scope with no way to tell
    it from a rejection — and the scope can be the whole catalogue.
    """
    client, database_path, product_model_id, _fake = setup_case(
        tmp_path, uncertain={"receipt_list"}
    )

    response = client.post("/api/inventory-sync")
    assert response.status_code in (200, 502)

    status, _error = _status_of(database_path, product_model_id)
    assert status == "NEEDS_REVIEW"


def test_the_audit_event_has_a_label_the_screen_can_show():
    """An audit event nobody can read is an audit event nobody checks.

    The backend writes INVENTORY_SYNC_UNKNOWN; the audit screen renders whatever
    ``AUDIT_EVENT_LABELS`` holds for the type. If the two drift the log still
    records the outcome correctly, but the operator sees a raw event name and
    scans past it — and the whole point of a fourth status is that it is legible.
    Asserted against the source rather than a rendered page because that is where
    the drift would start.
    """
    source = (ROOT / "frontend" / "apps" / "web" / "src" / "utils" / "format.ts").read_text(
        encoding="utf-8"
    )
    assert "INVENTORY_SYNC_UNKNOWN" in source, "the audit screen has no label for this event"
    assert "INVENTORY_SYNC_UNKNOWN: '库存同步结果未知'" in source


def test_a_timed_out_receipt_query_marks_the_run_not_just_the_rows(tmp_path):
    """The query is unknown, so the run itself must not be recorded as failed.

    Every per-product row goes to NEEDS_REVIEW, and the run's own record has to
    agree — otherwise the append-only audit chain says the sync failed and a
    retry is safe, which is the one instruction an unknown outcome must not
    carry. This is the second of the two paths that reach that state; the first
    is the fast-receive loop.
    """
    client, database_path, _product_model_id, _fake = setup_case(
        tmp_path, uncertain={"receipt_list"}
    )
    client.post("/api/inventory-sync")

    connection = connect_database(database_path)
    try:
        status = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key='inventory_sync.status'"
        ).fetchone()[0]
        assert status == "NEEDS_REVIEW", f"the run was recorded as {status!r}"
        event = connection.execute(
            "SELECT event_type FROM audit_events "
            "WHERE event_type LIKE 'INVENTORY_SYNC%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == "INVENTORY_SYNC_UNKNOWN"
    finally:
        connection.close()


def test_the_overall_summary_counts_a_unknown_product_as_not_pushed(tmp_path):
    """The one number the page shows must not silently drop the new status.

    inventory_sync_overall counted only FAILED, so a scope of three pushed and two
    unknown reported 失败 0 — the two that need checking in Lingxing became
    invisible in the only summary the operator reads. ``needsReview`` is broken out
    as well, because "failed" and "unknown" call for different actions: retry the
    first, look before touching the second.
    """
    client, database_path, product_model_id, _fake = setup_case(
        tmp_path, uncertain={"inventory_sync"}
    )
    client.post("/api/inventory-sync")

    board = client.get("/api/inventory-sync")
    assert board.status_code == 200
    overall = board.get_json()["data"]["overall"]
    assert overall["total"] >= 1
    assert overall["failed"] >= 1, "a 结果未知 product is not a pushed one"
    assert overall["needsReview"] >= 1, "the count the page needs must be there"
    assert overall["pushed"] + overall["failed"] + overall["pending"] == overall["total"]


def test_an_unknown_outcome_is_not_recorded_as_a_failure(tmp_path):
    """The audit chain is append-only, so the word in it is permanent.

    A run whose every product timed out used to write INVENTORY_SYNC_FAILED and
    status FAILED. That is a permanent record saying the sync failed, which the
    next operator reads as "sync it again" — the exact instruction the whole
    NEEDS_REVIEW distinction exists to withhold. The purchase-order pushes record
    PO_PUSH_UNKNOWN for the same reason; this is that, for the stock sync.
    """
    client, database_path, _product_model_id, _fake = setup_case(
        tmp_path, uncertain={"inventory_sync"}
    )
    client.post("/api/inventory-sync")

    connection = connect_database(database_path)
    try:
        status = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key='inventory_sync.status'"
        ).fetchone()[0]
        assert status == "NEEDS_REVIEW", f"the guard recorded {status!r}"

        event = connection.execute(
            "SELECT event_type, payload_json FROM audit_events "
            "WHERE event_type LIKE 'INVENTORY_SYNC%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == "INVENTORY_SYNC_UNKNOWN"
        assert '"NEEDS_REVIEW"' in event["payload_json"]
    finally:
        connection.close()


def test_a_definite_failure_is_still_recorded_as_a_failure(tmp_path):
    """The distinction only helps if the definite case keeps its word."""
    client, database_path, _product_model_id, _fake = setup_case(
        tmp_path, failures={"inventory_sync"}
    )
    client.post("/api/inventory-sync")

    connection = connect_database(database_path)
    try:
        status = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key='inventory_sync.status'"
        ).fetchone()[0]
        assert status == "FAILED"
        event = connection.execute(
            "SELECT event_type FROM audit_events "
            "WHERE event_type LIKE 'INVENTORY_SYNC%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == "INVENTORY_SYNC_FAILED"
    finally:
        connection.close()
