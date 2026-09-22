"""Property-based tests for the batch-traceability database migration.

These tests exercise the incremental ``user_version`` 11 -> 12 migration added
to :func:`traceability.db.initialize_database`. The migration is required to be
non-destructive: it may only *add* tables, columns and indexes, and must never
drop or rewrite existing historical data (Requirements 7.4, 8.1, 8.3).

To exercise the real 11 -> 12 migration code path with realistic, foreign-key
consistent historical data, each example:

1. builds a fully-migrated (v12) database via the application API and seeds it
   with historical per-unit (逐台) data (machines / product_code_sets /
   part_labels and their supporting rows);
2. *downgrades* that database to simulate a pre-migration v11 state by dropping
   the three v12-only tables and resetting ``user_version`` to 11;
3. snapshots every existing table;
4. re-runs ``create_app`` (which runs the migration under test); and
5. asserts every pre-existing table's row count did not decrease and that the
   historical per-unit rows are byte-for-byte unchanged and still queryable.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app


# Historical "逐台 / 逐部件" tables whose rows and field values must survive the
# migration completely unchanged (Requirements 7.4, 8.3).
_HISTORICAL_TABLES = (
    "machines",
    "product_code_sets",
    "part_labels",
    "product_code_batches",
    "trace_records",
    "trace_record_parts",
    "product_code_set_parts",
)

# Tables introduced by the v12 migration; they must not exist in the simulated
# pre-migration (v11) database.
_V12_TABLES = (
    "production_batch_supplier_consumption",
    "batch_trace_records",
    "production_batches",
)


def _snapshot(db_path: Path) -> dict[str, dict]:
    """Capture columns, ordered rows and row count for every user table."""

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        snapshot: dict[str, dict] = {}
        for table in table_names:
            columns = [
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            rows = [
                tuple(row[column] for column in columns)
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            ]
            rows.sort(key=lambda values: tuple(str(value) for value in values))
            snapshot[table] = {"columns": columns, "rows": rows, "count": len(rows)}
        return snapshot
    finally:
        connection.close()


def _user_version(db_path: Path) -> int:
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _seed_history(db_path: Path, component_count: int, quantity: int) -> None:
    """Create historical per-unit data through the API in a fresh v12 database."""

    app = create_app({"TESTING": True, "DATABASE": str(db_path)})
    client = app.test_client()

    components = [
        {
            "name": f"部件{index}",
            "supplierName": f"供应商{index}",
            "quantity": 1,
        }
        for index in range(component_count)
    ]
    product_response = client.post(
        "/api/products",
        json={"name": "历史走步机", "components": components},
    )
    assert product_response.status_code == 201, product_response.get_json()
    product = product_response.get_json()["data"]

    code_sets_response = client.post(
        f"/api/products/{product['id']}/code-sets",
        json={"prefix": "HIST", "quantity": quantity},
    )
    assert code_sets_response.status_code == 201, code_sets_response.get_json()


def _downgrade_to_v11(db_path: Path) -> None:
    """Drop the v12-only tables and reset ``user_version`` to simulate v11."""

    connection = sqlite3.connect(str(db_path))
    try:
        for table in _V12_TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("PRAGMA user_version = 11")
        connection.commit()
    finally:
        connection.close()


# Feature: batch-traceability, Property 23: 迁移只读保留历史数据
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    component_count=st.integers(min_value=1, max_value=3),
    quantity=st.integers(min_value=1, max_value=4),
)
def test_migration_preserves_read_only_history(component_count: int, quantity: int) -> None:
    """v11 -> v12 migration keeps every existing table's rows and per-unit history.

    Validates: Requirements 7.4, 8.1, 8.3
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-mig-"))
    db_path = workdir / "traceability.db"
    try:
        # 1) Build a v12 database with historical per-unit data, then downgrade
        #    it to a simulated pre-migration v11 state.
        _seed_history(db_path, component_count, quantity)
        _downgrade_to_v11(db_path)

        assert _user_version(db_path) == 11
        before = _snapshot(db_path)
        # The v12-only tables must be absent from the pre-migration snapshot.
        for table in _V12_TABLES:
            assert table not in before

        # 2) Run the migration under test.
        create_app({"TESTING": True, "DATABASE": str(db_path)})

        after = _snapshot(db_path)

        # 3a) Migration lands on the fixed target version (Requirement 8.2 context).
        assert _user_version(db_path) == 22

        # 3b) Every pre-existing table's row count must not decrease (Req 8.1).
        for table, before_state in before.items():
            assert table in after, f"existing table {table} disappeared after migration"
            assert after[table]["count"] >= before_state["count"], (
                f"table {table} lost rows: "
                f"{before_state['count']} -> {after[table]['count']}"
            )

        # 3c) Historical per-unit rows and field values are unchanged and still
        #     queryable (Req 7.4, 8.3).
        for table in _HISTORICAL_TABLES:
            if table not in before:
                continue
            assert after[table]["columns"] == before[table]["columns"]
            assert after[table]["rows"] == before[table]["rows"], (
                f"historical table {table} changed during migration"
            )

        # 3d) The v12 migration only *adds* structure; the historical machines
        #     (single-unit QR codes) remain queryable with identical identity.
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        try:
            identity_codes = [
                row["identification_code"]
                for row in connection.execute(
                    "SELECT identification_code FROM machines ORDER BY id"
                ).fetchall()
            ]
        finally:
            connection.close()
        assert len(identity_codes) == before["machines"]["count"]
        assert all(code.startswith("PTS:M:") for code in identity_codes)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 24: 迁移回滚原子性
# ---------------------------------------------------------------------------

import pytest

import traceability.db as db_module


# The v12-only column added to an existing table by the migration under test.
_V12_COLUMN_TABLE = "supplier_inventory_movements"
_V12_COLUMN_NAME = "production_batch_id"

# A DDL/DML statement that always fails when executed inside the migration
# transaction (the referenced table never exists), used to inject a mid-migration
# failure at an arbitrary point among the v12 statements.
_FAILING_STATEMENT = "INSERT INTO ___pts_migration_no_such_table___ (id) VALUES (1)"


def _table_exists(db_path: Path, table: str) -> bool:
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _column_exists(db_path: Path, table: str, column: str) -> bool:
    connection = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        return column in columns
    finally:
        connection.close()


def _downgrade_to_v11_full(db_path: Path) -> None:
    """Simulate a genuine pre-migration v11 state.

    In addition to dropping the three v12-only tables (as
    :func:`_downgrade_to_v11` does), this also removes the v12-only
    ``supplier_inventory_movements.production_batch_id`` column so that the
    baseline truly lacks *every* structural change the migration introduces.
    This lets the rollback assertions prove that no new table *or* column
    residue survives an injected failure.
    """

    connection = sqlite3.connect(str(db_path))
    try:
        for table in _V12_TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        columns = {
            row[1]
            for row in connection.execute(
                f"PRAGMA table_info({_V12_COLUMN_TABLE})"
            ).fetchall()
        }
        if _V12_COLUMN_NAME in columns:
            connection.execute(
                f"ALTER TABLE {_V12_COLUMN_TABLE} DROP COLUMN {_V12_COLUMN_NAME}"
            )
        connection.execute("PRAGMA user_version = 11")
        connection.commit()
    finally:
        connection.close()


# Feature: batch-traceability, Property 24: 迁移回滚原子性
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    # A failure point among the v12 statements; the extra value at the end
    # (== len(statements)) injects the failure at the trailing column-add step,
    # i.e. *after* every new table has already been created in the transaction.
    failure_point=st.integers(
        min_value=0, max_value=len(db_module.BATCH_TRACEABILITY_V12_STATEMENTS)
    ),
    component_count=st.integers(min_value=1, max_value=2),
    quantity=st.integers(min_value=1, max_value=3),
)
def test_migration_rollback_is_atomic(
    failure_point: int, component_count: int, quantity: int
) -> None:
    """An injected failure at any migration step rolls back atomically.

    For any failure point injected during the v11 -> v12 migration, the
    transaction rolls back so that ``user_version``, table structure and data
    all remain exactly as they were before the migration (no new table/column
    residue, no data changes), and the migration failure surfaces as an error.

    Validates: Requirements 8.4
    """

    statements = db_module.BATCH_TRACEABILITY_V12_STATEMENTS
    inject_at_column_step = failure_point == len(statements)

    workdir = Path(tempfile.mkdtemp(prefix="pts-mig-rb-"))
    db_path = workdir / "traceability.db"
    try:
        # 1) Build a v12 database with historical per-unit data, then downgrade
        #    it to a genuine simulated pre-migration v11 state (no v12 tables and
        #    no v12 column).
        _seed_history(db_path, component_count, quantity)
        _downgrade_to_v11_full(db_path)

        assert _user_version(db_path) == 11
        for table in _V12_TABLES:
            assert not _table_exists(db_path, table)
        assert not _column_exists(db_path, _V12_COLUMN_TABLE, _V12_COLUMN_NAME)

        before = _snapshot(db_path)

        # 2) Run the migration under test with a failure injected at the chosen
        #    point, and assert startup aborts by surfacing the error (Req 8.4).
        if inject_at_column_step:
            # Fail exactly at the trailing column-add step: every v12 table has
            # already been created inside the open transaction at this point.
            original_ensure_column = db_module._ensure_column

            def _failing_ensure_column(connection, table, column, definition):
                if table == _V12_COLUMN_TABLE and column == _V12_COLUMN_NAME:
                    raise RuntimeError("injected migration failure at column step")
                return original_ensure_column(connection, table, column, definition)

            with (
                pytest.raises(RuntimeError),
                pytest.MonkeyPatch.context() as patcher,
            ):
                patcher.setattr(db_module, "_ensure_column", _failing_ensure_column)
                create_app({"TESTING": True, "DATABASE": str(db_path)})
        else:
            patched_statements = (
                statements[:failure_point]
                + (_FAILING_STATEMENT,)
                + statements[failure_point:]
            )
            with (
                pytest.raises(sqlite3.OperationalError),
                pytest.MonkeyPatch.context() as patcher,
            ):
                patcher.setattr(
                    db_module,
                    "BATCH_TRACEABILITY_V12_STATEMENTS",
                    patched_statements,
                )
                create_app({"TESTING": True, "DATABASE": str(db_path)})

        # 3a) Version is rolled back and stays at the pre-migration value.
        assert _user_version(db_path) == 11

        # 3b) No new table residue survives the rolled-back migration.
        for table in _V12_TABLES:
            assert not _table_exists(db_path, table), (
                f"v12 table {table} leaked after a rolled-back migration"
            )

        # 3c) No new column residue survives the rolled-back migration.
        assert not _column_exists(db_path, _V12_COLUMN_TABLE, _V12_COLUMN_NAME), (
            f"v12 column {_V12_COLUMN_TABLE}.{_V12_COLUMN_NAME} leaked after rollback"
        )

        # 3d) Every pre-existing table's structure and data are byte-for-byte
        #     identical to the pre-migration snapshot (no data changes).
        after = _snapshot(db_path)
        assert after == before, "database changed after a rolled-back migration"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 25: 迁移跳过幂等
# ---------------------------------------------------------------------------


# Feature: batch-traceability, Property 25: 迁移跳过幂等
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    component_count=st.integers(min_value=1, max_value=3),
    quantity=st.integers(min_value=1, max_value=4),
    reruns=st.integers(min_value=1, max_value=3),
)
def test_migration_skip_is_idempotent(
    component_count: int, quantity: int, reruns: int
) -> None:
    """Re-initializing an already-current database is a no-op.

    When startup detects ``user_version`` already equal to (or above) the
    current target, the migration must be skipped and make no change to the
    database structure or data: ``user_version`` stays at 22 and the
    whole-database snapshot is byte-for-byte identical across arbitrarily many
    re-initializations.

    Validates: Requirements 8.5
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-mig-skip-"))
    db_path = workdir / "traceability.db"
    try:
        # 1) Build a fully-migrated current database with historical per-unit data.
        #    _seed_history runs create_app through every migration to v14.
        _seed_history(db_path, component_count, quantity)

        assert _user_version(db_path) == 22
        before = _snapshot(db_path)
        # The v12 tables exist in the already-migrated baseline.
        for table in _V12_TABLES:
            assert table in before

        # 2) Re-run initialization one or more times on the current database.
        #    Each run must observe user_version >= 14 and skip all migrations.
        for _ in range(reruns):
            create_app({"TESTING": True, "DATABASE": str(db_path)})

            # 3a) Version is unchanged: the skip never bumps or rewrites it.
            assert _user_version(db_path) == 22

            # 3b) The whole-database snapshot is unchanged (no structure/data
            #     changes of any kind), i.e. the migration was a true no-op.
            after = _snapshot(db_path)
            assert after == before, (
                "re-initializing an already-v12 database changed the database"
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Unit tests: target version & fail-fast startup abort (Requirements 8.2, 8.6)
#
# These are plain example-based unit tests (not property tests). They reuse the
# module-level helpers above (_user_version / _table_exists / _V12_TABLES and
# the db_module / create_app imports) rather than re-deriving them.
# ---------------------------------------------------------------------------


def test_successful_migration_lands_on_current_user_version() -> None:
    """A successful fresh migration reaches the current schema version.

    A brand-new database has no per-feature version, so ``create_app`` runs the
    baseline plus all incremental migrations end to end. Once it completes
    successfully the database must report the fixed target version 19.

    Validates: Requirements 8.2
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-mig-target-"))
    db_path = workdir / "traceability.db"
    try:
        # Fresh database: create_app runs the full migration path to the target.
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})

        # The migration lands on the fixed target version (Req 8.2).
        assert _user_version(db_path) == 22

        # And the resulting service is actually usable end to end.
        for table in _V12_TABLES:
            assert _table_exists(db_path, table), f"v12 table {table} missing after migration"
        client = app.test_client()
        health = client.get("/api/health")
        assert health.status_code == 200, health.get_json()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_migration_step_failure_aborts_startup_without_usable_service(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """A failing migration step re-raises and yields no usable service.

    When a step of the v11 -> v12 migration fails, ``initialize_database`` must
    roll back and re-raise; because ``create_app`` calls it during construction,
    the exception propagates out of ``create_app`` and no Flask application (no
    usable service) is returned. The database is left at the pre-migration
    version 11 with none of the v12 tables (Req 8.4 rollback backing 8.6).

    Validates: Requirements 8.6
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-mig-abort-"))
    db_path = workdir / "traceability.db"
    try:
        # Inject a guaranteed failure into the v12 migration statement batch so
        # a real migration step raises mid-transaction.
        patched_statements = db_module.BATCH_TRACEABILITY_V12_STATEMENTS + (
            _FAILING_STATEMENT,
        )
        monkeypatch.setattr(
            db_module, "BATCH_TRACEABILITY_V12_STATEMENTS", patched_statements
        )

        # initialize_database must re-raise, so create_app never returns an app.
        app = None
        with pytest.raises(sqlite3.OperationalError):
            app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        assert app is None, "create_app returned a service despite a failed migration"

        # Startup aborted cleanly: version stayed pre-migration and no v12 table
        # was left behind, so no usable batch-traceability service exists.
        assert _user_version(db_path) == 11
        for table in _V12_TABLES:
            assert not _table_exists(db_path, table), (
                f"v12 table {table} leaked despite aborted startup"
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
