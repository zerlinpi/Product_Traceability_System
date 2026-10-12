"""A timeout on a Lingxing push is 结果未知, and the database now says so.

Before v23 the only statuses were PENDING / PUSHED / FAILED, so a push whose
reply was lost had to be recorded as FAILED — a word that reads as an
instruction to retry, and a retry can create the order twice. These tests hold
down both halves: the migration that widened the CHECK constraint, and the two
push paths that now write NEEDS_REVIEW.
"""

from __future__ import annotations

import sqlite3

from app import create_app
from batch_strategies import build_product_scenario
from capability_helpers import FakeLingxingService
from test_inbound_receipts import create_order
from traceability.db import SCHEMA_VERSION, connect_database


def setup_case(tmp_path, *, uncertain=(), failures=()):
    """An app whose Lingxing transport fails the way the test asks it to."""
    database_path = tmp_path / "needs-review.db"
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
    scenario = build_product_scenario(client, "NR", [1], 100)
    return client, database_path, scenario, fake


def create_receipt(client, order, quantity=5):
    response = client.post(
        "/api/inbound-receipts",
        json={"purchaseOrderId": order["id"], "quantity": quantity},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# --------------------------------------------------------------------------
# The push paths
# --------------------------------------------------------------------------


def test_a_timed_out_purchase_order_push_is_recorded_as_needs_review(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path, uncertain={"purchase_order"})
    order = create_order(client, scenario)

    response = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert response.status_code >= 400, response.get_json()

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT sync_status, push_error, push_in_progress FROM purchase_orders WHERE id=?",
            (order["id"],),
        ).fetchone()
        assert row["sync_status"] == "NEEDS_REVIEW", "超时被记成了普通失败"
        assert "结果未知" in row["push_error"], "没有告诉操作员下一步该做什么"
        assert row["push_in_progress"] == 0, "推送守卫没有释放"

        event = database.execute(
            "SELECT event_type, payload_json FROM audit_events WHERE event_type LIKE 'PO_PUSH%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["event_type"] == "PO_PUSH_UNKNOWN"
        assert '"UNKNOWN"' in event["payload_json"]
    finally:
        database.close()


def test_a_timed_out_receipt_push_is_recorded_as_needs_review(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path, uncertain={"inbound_receipt"})
    order = create_order(client, scenario)
    receipt = create_receipt(client, order)
    # A receipt may only be pushed once its purchase order went out, so that push
    # has to succeed first — only the receipt's own push is the timed-out one.
    assert client.post(f"/api/purchase-orders/{order['id']}/push").status_code == 200

    client.post(f"/api/inbound-receipts/{receipt['id']}/push")

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT sync_status, push_error FROM inbound_receipts WHERE id=?",
            (receipt["id"],),
        ).fetchone()
        assert row["sync_status"] == "NEEDS_REVIEW"
        assert "结果未知" in row["push_error"]
    finally:
        database.close()


def test_a_definite_rejection_is_still_a_plain_failure(tmp_path):
    """The distinction has to be earned: a refusal really did not write."""
    client, database_path, scenario, _fake = setup_case(tmp_path, failures={"purchase_order"})
    order = create_order(client, scenario)

    client.post(f"/api/purchase-orders/{order['id']}/push")

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT sync_status, push_error FROM purchase_orders WHERE id=?", (order["id"],)
        ).fetchone()
        assert row["sync_status"] == "FAILED"
        assert "结果未知" not in row["push_error"]
    finally:
        database.close()


def test_a_timed_out_order_can_be_found_by_status(tmp_path):
    """The list filter accepts the new value — otherwise nobody could find one."""
    client, _database_path, scenario, _fake = setup_case(tmp_path, uncertain={"purchase_order"})
    order = create_order(client, scenario)
    client.post(f"/api/purchase-orders/{order['id']}/push")

    response = client.get("/api/purchase-orders?syncStatus=NEEDS_REVIEW")
    assert response.status_code == 200, response.get_json()
    assert order["poNo"] in [row["poNo"] for row in response.get_json()["data"]]


# --------------------------------------------------------------------------
# The constraint
# --------------------------------------------------------------------------


def test_the_database_refuses_a_status_it_does_not_know(tmp_path):
    _client, database_path, scenario, _fake = setup_case(tmp_path)
    connection = sqlite3.connect(str(database_path))
    try:
        try:
            connection.execute(
                "INSERT INTO purchase_orders(po_no, created_at, sync_status) "
                "VALUES ('PO-BOGUS', 'now', 'MADE_UP')"
            )
        except sqlite3.IntegrityError:
            return
        raise AssertionError("CHECK 约束没有拦住未知状态")
    finally:
        connection.close()


def _rebuild_as_v22(connection: sqlite3.Connection) -> None:
    """Replace the two push tables with their pre-v23 (three-value) form.

    Constructed rather than restored from a fixture so the upgrade under test is
    a real one: three-value tables in, four-value tables out, rows preserved.
    """
    connection.execute("DROP TABLE IF EXISTS inbound_receipts")
    connection.execute("DROP TABLE IF EXISTS purchase_orders")
    connection.execute(
        """
        CREATE TABLE purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_no TEXT NOT NULL COLLATE NOCASE UNIQUE,
            supplier_id INTEGER,
            part_type_id INTEGER,
            product_model_id INTEGER,
            quantity INTEGER,
            fields_json TEXT NOT NULL DEFAULT '{}',
            sync_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
            push_in_progress INTEGER NOT NULL DEFAULT 0,
            lingxing_po_id TEXT NOT NULL DEFAULT '',
            lingxing_raw_response TEXT NOT NULL DEFAULT '',
            push_error TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL,
            pushed_at TEXT,
            push_started_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE inbound_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_order_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999999),
            receiver TEXT NOT NULL DEFAULT '',
            receiver_user_id INTEGER,
            received_at TEXT NOT NULL,
            sync_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
            push_in_progress INTEGER NOT NULL DEFAULT 0,
            lingxing_inbound_id TEXT NOT NULL DEFAULT '',
            lingxing_raw_response TEXT NOT NULL DEFAULT '',
            push_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            pushed_at TEXT,
            push_started_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO purchase_orders(po_no, quantity, sync_status, created_at) "
        "VALUES ('PO-OLD', 3, 'FAILED', '2026-01-01')"
    )
    connection.execute(
        "INSERT INTO inbound_receipts(purchase_order_id, quantity, received_at, sync_status, created_at) "
        "VALUES (1, 3, '2026-01-02', 'PUSHED', '2026-01-02')"
    )
    connection.execute("PRAGMA user_version = 22")
    connection.commit()


def test_an_old_database_upgrades_without_losing_its_rows(tmp_path):
    database_path = tmp_path / "old.db"
    create_app({"TESTING": True, "DATABASE": str(database_path)})
    connection = sqlite3.connect(str(database_path))
    try:
        _rebuild_as_v22(connection)
        # Confirm the fixture really is the old shape, or the test proves nothing.
        assert "NEEDS_REVIEW" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='purchase_orders'"
        ).fetchone()[0]
    finally:
        connection.close()

    create_app({"TESTING": True, "DATABASE": str(database_path)})

    connection = connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "NEEDS_REVIEW" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='purchase_orders'"
        ).fetchone()[0]
        assert "NEEDS_REVIEW" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='inbound_receipts'"
        ).fetchone()[0]
        # Rows survive verbatim.
        assert connection.execute(
            "SELECT sync_status FROM purchase_orders WHERE po_no='PO-OLD'"
        ).fetchone()[0] == "FAILED"
        assert connection.execute("SELECT sync_status FROM inbound_receipts").fetchone()[0] == "PUSHED"
        # And the new value is now writable.
        connection.execute(
            "UPDATE purchase_orders SET sync_status='NEEDS_REVIEW' WHERE po_no='PO-OLD'"
        )
        assert connection.execute(
            "SELECT sync_status FROM purchase_orders WHERE po_no='PO-OLD'"
        ).fetchone()[0] == "NEEDS_REVIEW"
    finally:
        connection.close()
