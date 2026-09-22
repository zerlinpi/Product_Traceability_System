"""Tamper-evident audit ledger.

``audit_events`` is the evidence chain for product traceability. As a plain table
it offered no protection: anyone with a SQLite client could UPDATE or DELETE a
row and every query would keep answering confidently. These tests pin the hash
chain, the append-only triggers, the v21 backfill, and the verification command.

Feature: batch-traceability, Property 73: 审计账本不可篡改
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402
from capability_helpers import PASSWORD, bootstrap_admin, make_auth_app  # noqa: E402
from traceability.audit_chain import (  # noqa: E402
    GENESIS_HASH,
    HASHED_FIELDS,
    canonical_entry,
    chain_tip,
    compute_event_hash,
    link_event,
    verify_chain,
)
from traceability.backup import create_backup, restore_backup  # noqa: E402
from traceability.db import connect_database  # noqa: E402

FIXED_NOW = "2026-07-22T10:30:00+08:00"


def _fields(index: int, **overrides) -> dict:
    base = {
        "event_id": f"EV-{index:04d}",
        "event_type": "TEST_EVENT",
        "object_type": "TEST",
        "object_code": f"CODE-{index}",
        "related_object_code": "",
        "station_id": "",
        "station_name": "",
        "operator_name": "测试员",
        "actor_user_id": None,
        "reason": "",
        "payload_json": "{}",
        "occurred_at": FIXED_NOW,
    }
    base.update(overrides)
    return base


def _append(connection: sqlite3.Connection, index: int, **overrides) -> str:
    fields = _fields(index, **overrides)
    prev, digest = link_event(connection, fields)
    connection.execute(
        """
        INSERT INTO audit_events(
            event_id, event_type, object_type, object_code, related_object_code,
            station_id, station_name, operator_name, actor_user_id, reason,
            payload_json, occurred_at, prev_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fields["event_id"], fields["event_type"], fields["object_type"],
            fields["object_code"], fields["related_object_code"], fields["station_id"],
            fields["station_name"], fields["operator_name"], fields["actor_user_id"],
            fields["reason"], fields["payload_json"], fields["occurred_at"], prev, digest,
        ),
    )
    return digest


@pytest.fixture()
def database(tmp_path):
    """A migrated database at the current schema version."""
    _app, database_path, _fake = make_auth_app(tmp_path)
    connection = connect_database(database_path)
    yield connection
    connection.close()


@pytest.fixture()
def database_path_of(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    return database_path


# --------------------------------------------------------------------------
# 1. Hashing
# --------------------------------------------------------------------------


def test_canonical_entry_is_deterministic():
    fields = _fields(1)
    assert canonical_entry(fields) == canonical_entry(dict(fields))


def test_canonical_entry_treats_none_and_empty_string_alike():
    """``actor_user_id`` is NULL for system actions and '' after some round
    trips; they must not hash differently."""
    left = _fields(1, actor_user_id=None)
    right = _fields(1, actor_user_id="")
    assert canonical_entry(left) == canonical_entry(right)


def test_canonical_entry_covers_every_hashed_field():
    rendered = canonical_entry(_fields(1))
    for name in HASHED_FIELDS:
        assert f"{name}=" in rendered


def test_the_hash_depends_on_the_previous_hash():
    fields = _fields(1)
    assert compute_event_hash("a" * 64, fields) != compute_event_hash("b" * 64, fields)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "OTHER"),
        ("event_type", "OTHER"),
        ("object_type", "OTHER"),
        ("object_code", "OTHER"),
        ("related_object_code", "OTHER"),
        ("station_id", "OTHER"),
        ("station_name", "OTHER"),
        ("operator_name", "OTHER"),
        ("actor_user_id", 7),
        ("reason", "OTHER"),
        ("payload_json", '{"a":1}'),
        ("occurred_at", "2027-01-01T00:00:00+08:00"),
    ],
)
def test_the_hash_covers_every_field(field, value):
    baseline = compute_event_hash(GENESIS_HASH, _fields(1))
    assert compute_event_hash(GENESIS_HASH, _fields(1, **{field: value})) != baseline


def test_the_hash_is_a_sha256_hex_digest():
    digest = compute_event_hash(GENESIS_HASH, _fields(1))
    assert len(digest) == 64
    int(digest, 16)


# --------------------------------------------------------------------------
# 2. Linking
# --------------------------------------------------------------------------


def test_chain_tip_on_an_empty_ledger_is_the_genesis_value(database):
    assert chain_tip(database) == GENESIS_HASH


def test_link_event_returns_the_previous_tip(database):
    first = _append(database, 1)
    _prev, second = link_event(database, _fields(2))
    assert chain_tip(database) == first
    assert second != first


def test_link_event_does_not_commit_a_callers_transaction(database):
    """Most callers already hold a write transaction; the chain must join it,
    not silently commit their work."""
    database.execute("BEGIN IMMEDIATE")
    database.execute(
        "INSERT INTO suppliers(supplier_code, name, contact, phone, created_at, updated_at) "
        "VALUES ('SUP-TX', '事务测试', 'x', '1', 'now', 'now')"
    )
    link_event(database, _fields(1))
    assert database.in_transaction is True
    database.execute("ROLLBACK")
    assert database.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0


def test_link_event_opens_a_transaction_when_the_caller_has_none(database):
    assert database.in_transaction is False
    link_event(database, _fields(1))
    assert database.in_transaction is False, "the short transaction must be closed again"


# --------------------------------------------------------------------------
# 3. Verification
# --------------------------------------------------------------------------


def test_an_empty_ledger_verifies(database):
    report = verify_chain(database)
    assert report.ok and report.total == 0


def test_a_valid_chain_verifies(database):
    for index in range(1, 6):
        _append(database, index)
    report = verify_chain(database)
    assert report.ok
    assert report.verified == 5
    assert report.total == 5
    assert report.first_broken_id is None


def test_a_modified_field_breaks_the_chain(database):
    for index in range(1, 4):
        _append(database, index)
    # Drop the guard the way a tamperer would, then edit a row.
    database.execute("DROP TRIGGER audit_events_no_update")
    database.execute("UPDATE audit_events SET operator_name = '别人' WHERE event_id = 'EV-0002'")

    report = verify_chain(database)
    assert not report.ok
    assert report.first_broken_id == 2
    assert "内容哈希不匹配" in report.first_broken_reason


def test_a_deleted_row_breaks_the_chain(database):
    for index in range(1, 5):
        _append(database, index)
    database.execute("DROP TRIGGER audit_events_no_delete")
    database.execute("DELETE FROM audit_events WHERE event_id = 'EV-0002'")

    report = verify_chain(database)
    assert not report.ok
    # The break shows up at the row that followed the deleted one.
    assert report.first_broken_id == 3
    assert "prev_hash" in report.first_broken_reason


def test_deleting_the_last_row_is_detected(database):
    """Truncating the tail leaves a shorter but internally consistent chain.

    It is caught because the verifier records the tip, so a later comparison
    against a known-good tip exposes it — and the deletion itself is blocked by
    the trigger. Here we assert the trigger is what stops it.
    """
    for index in range(1, 4):
        _append(database, index)
    before = verify_chain(database)
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("DELETE FROM audit_events WHERE id = (SELECT MAX(id) FROM audit_events)")
    after = verify_chain(database)
    assert after.total == before.total
    assert after.tip == before.tip


def test_an_inserted_row_breaks_the_chain(database):
    """A forged row cannot be spliced in: its prev_hash will not match."""
    for index in range(1, 4):
        _append(database, index)
    database.execute(
        "INSERT INTO audit_events(event_id, event_type, object_type, object_code, "
        "operator_name, payload_json, occurred_at, prev_hash, event_hash) "
        "VALUES ('FORGED', 'TEST_EVENT', 'TEST', 'X', '攻击者', '{}', ?, 'deadbeef', 'cafebabe')",
        (FIXED_NOW,),
    )
    report = verify_chain(database)
    assert not report.ok
    assert report.first_broken_id is not None


def test_rows_without_a_hash_are_reported_as_unhashed(database):
    database.execute(
        "INSERT INTO audit_events(event_id, event_type, object_type, object_code, "
        "operator_name, payload_json, occurred_at) "
        "VALUES ('LEGACY', 'TEST_EVENT', 'TEST', 'X', 'op', '{}', ?)",
        (FIXED_NOW,),
    )
    report = verify_chain(database)
    assert report.unhashed == 1
    assert not report.ok
    assert any("未纳入哈希链" in problem for problem in report.problems)


def test_verification_reports_the_tip(database):
    last = _append(database, 1)
    assert verify_chain(database).tip == last


# --------------------------------------------------------------------------
# 4. Append-only triggers
# --------------------------------------------------------------------------


def test_update_is_refused(database):
    _append(database, 1)
    with pytest.raises(sqlite3.IntegrityError) as error:
        database.execute("UPDATE audit_events SET operator_name = 'x' WHERE event_id = 'EV-0001'")
    assert "只追加账本" in str(error.value)


def test_delete_is_refused(database):
    _append(database, 1)
    with pytest.raises(sqlite3.IntegrityError) as error:
        database.execute("DELETE FROM audit_events WHERE event_id = 'EV-0001'")
    assert "只追加账本" in str(error.value)


def test_both_triggers_exist(database):
    names = {
        str(row[0])
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_events'"
        ).fetchall()
    }
    assert {"audit_events_no_update", "audit_events_no_delete"} <= names


def test_insert_is_still_allowed(database):
    _append(database, 1)
    _append(database, 2)
    assert database.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2


# --------------------------------------------------------------------------
# 5. Migration and backfill
# --------------------------------------------------------------------------


def test_migration_v21_adds_the_columns(database_path_of):
    connection = connect_database(database_path_of)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()
        }
        assert {"prev_hash", "event_hash"} <= columns
    finally:
        connection.close()


def test_migration_backfills_pre_existing_rows(tmp_path):
    """Rows written before v21 must end up inside the chain, not outside it."""
    app, database_path, _fake = make_auth_app(tmp_path)

    connection = connect_database(database_path)
    try:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute("DROP TRIGGER audit_events_no_delete")
        for index in range(1, 4):
            connection.execute(
                "INSERT INTO audit_events(event_id, event_type, object_type, object_code, "
                "operator_name, payload_json, occurred_at) VALUES (?, 'LEGACY', 'T', ?, 'op', '{}', ?)",
                (f"OLD-{index}", f"C{index}", FIXED_NOW),
            )
        connection.execute("PRAGMA user_version = 20")
    finally:
        connection.close()

    # Re-initialising runs the v21 migration again.
    from app import create_app

    create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "SECRET_KEY": "audit-backfill",
            "NOW_PROVIDER": lambda: FIXED_NOW,
        }
    )

    connection = connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        report = verify_chain(connection)
        assert report.total == 3
        assert report.unhashed == 0, "the backfill did not cover the legacy rows"
        assert report.ok
        # And the guards are back in place.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_events")
    finally:
        connection.close()


def test_the_migration_is_idempotent(tmp_path):
    """Re-running initialisation must not corrupt an already-migrated ledger."""
    app, database_path, _fake = make_auth_app(tmp_path)
    connection = connect_database(database_path)
    try:
        _append(connection, 1)
        before = verify_chain(connection)
    finally:
        connection.close()

    from app import create_app

    for _ in range(2):
        create_app(
            {
                "TESTING": True,
                "DATABASE": str(database_path),
                "SECRET_KEY": "audit-idempotent",
                "NOW_PROVIDER": lambda: FIXED_NOW,
            }
        )

    connection = connect_database(database_path)
    try:
        after = verify_chain(connection)
        assert after.ok
        assert after.total == before.total
        assert after.tip == before.tip, "a re-run rewrote the chain"
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 6. Real application writes
# --------------------------------------------------------------------------


def test_application_writes_produce_a_verifiable_chain(tmp_path):
    """Bootstrap + login + user management all go through the ledger."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    created = admin.post(
        "/api/users",
        json={
            "username": "audit-warehouse",
            "displayName": "审计仓管",
            "password": PASSWORD,
            "role": "WAREHOUSE",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.get_json()

    connection = connect_database(database_path)
    try:
        report = verify_chain(connection)
        assert report.total >= 2, "expected at least the login and user-created events"
        assert report.unhashed == 0
        assert report.ok, report.first_broken_reason
        types = {
            row[0]
            for row in connection.execute("SELECT DISTINCT event_type FROM audit_events").fetchall()
        }
        assert "USER_LOGIN" in types
        assert "USER_CREATED" in types
    finally:
        connection.close()


def test_business_writes_are_chained(tmp_path):
    """A product creation writes an audit event through record_audit_event."""
    from capability_helpers import post, seed_scenario

    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    seed_scenario(admin, csrf, "AUDIT")

    connection = connect_database(database_path)
    try:
        report = verify_chain(connection)
        assert report.unhashed == 0
        assert report.ok, report.first_broken_reason
        assert report.verified >= 3
    finally:
        connection.close()


def test_a_login_outside_a_transaction_does_not_break_the_chain(tmp_path):
    """``_record_security_event`` runs in autocommit during login; the chain has
    to open its own short transaction there."""
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)

    from capability_helpers import insert_user, login

    insert_user(database_path, "audit-op", "OPERATIONS")
    for _ in range(3):
        login(app, "audit-op")

    connection = connect_database(database_path)
    try:
        report = verify_chain(connection)
        assert report.ok, report.first_broken_reason
        assert report.unhashed == 0
    finally:
        connection.close()


def test_the_chain_survives_a_backup_restore_round_trip(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)

    connection = connect_database(database_path)
    try:
        assert verify_chain(connection).ok
    finally:
        connection.close()

    report = create_backup(database_path, tmp_path / "bk", created_at=FIXED_NOW)
    restore_backup(report.path, database_path, created_at=FIXED_NOW, keep_safety_copy=False)

    connection = connect_database(database_path)
    try:
        after = verify_chain(connection)
        assert after.ok
        assert after.total > 0
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 7. Command line
# --------------------------------------------------------------------------


def test_cli_audit_verify_passes_on_a_healthy_ledger(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    assert manage.main(["--database", str(database_path), "audit-verify"]) == 0
    assert "哈希链完整" in capsys.readouterr().out


def test_cli_audit_verify_fails_on_a_broken_ledger(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    connection = connect_database(database_path)
    try:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute("UPDATE audit_events SET operator_name = '篡改' WHERE id = (SELECT MIN(id) FROM audit_events)")
    finally:
        connection.close()

    assert manage.main(["--database", str(database_path), "audit-verify"]) == 1
    output = capsys.readouterr().out
    assert "已断裂" in output
    assert "首个异常记录" in output


def test_cli_audit_verify_on_a_missing_database(tmp_path, capsys):
    assert manage.main(["--database", str(tmp_path / "nope.db"), "audit-verify"]) == 1


def test_cli_audit_info(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    assert manage.main(["--database", str(database_path), "audit-info"]) == 0
    output = capsys.readouterr().out
    assert "审计账本信息" in output
    assert "只追加触发器" in output
    assert "已启用" in output
