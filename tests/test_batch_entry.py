"""Property-based tests for batch scan registration (BatchEntryService).

These tests drive the ``POST /api/batch-entry/scan`` endpoint (app.py, task
9.1) which lets a warehouse operator scan a batch QR to register the whole
production batch in one shot (Requirements 2.1, 2.2).

Property 5 asserts the structural invariant of a successful scan registration:
scanning a batch that has no prior registration creates **exactly one** new
``batch_trace_records`` row; with the quantity omitted the record's
``registered_quantity`` defaults to the batch's ``planned_quantity``; and —
because batch registration produces a single batch-level record rather than
per-unit records — the legacy per-unit ``machines`` and ``trace_records`` table
counts are left unchanged.

The scenario (product + supplier stock + per-unit usage) is assembled via the
admin API using the shared builders in ``tests/batch_strategies.py`` following
the ``tests/test_batch_generation.py`` conventions for app / DB setup.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from traceability.db import connect_database

from batch_strategies import (
    build_product_scenario,
    entity_code_prefixes,
    per_unit_usages,
    quantities_including_boundaries,
)


# Supplier inventory batches accept at most 10,000,000 units on creation, so we
# keep planned quantities within a range whose worst-case deduction
# (``quantity × max(usage)``) plus slack stays under that cap.
_MAX_PLANNED_QUANTITY = 2_000_000


def _bounded_quantities() -> st.SearchStrategy[int]:
    """Valid planned quantities (incl. boundaries) below the stock cap."""

    return quantities_including_boundaries().filter(
        lambda value: value <= _MAX_PLANNED_QUANTITY
    )


def _count(connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# Feature: batch-traceability, Property 5: 扫码登记形成单条批次记录
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_scan_registration_forms_single_batch_record(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """恰新增一条记录，缺省 registered_quantity == planned_quantity，machines / trace_records 计数不变。

    Validates: Requirements 2.1, 2.2
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-entry-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed each supplier batch with enough stock for the worst-case part so
        # generation succeeds, then generate the batch to be scanned.
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "ENTRY", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        generate = client.post(
            "/api/production-batches",
            json={
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": prefix,
            },
        )
        assert generate.status_code == 201, generate.get_json()
        created = generate.get_json()["data"]
        batch_code = created["batchCode"]
        planned_quantity = created["plannedQuantity"]

        # Snapshot the counts that a scan must (or must not) change.
        connection = connect_database(str(db_path))
        try:
            before_batch_records = _count(connection, "batch_trace_records")
            before_machines = _count(connection, "machines")
            before_trace_records = _count(connection, "trace_records")
        finally:
            connection.close()

        # Scan with the quantity omitted so registered_quantity must default to
        # the batch's planned quantity (Requirement 2.1).
        response = client.post(
            "/api/batch-entry/scan",
            json={"code": batch_code},
        )

        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["ok"] is True
        record = body["data"]

        # (A) With quantity omitted, the registered quantity defaults to the
        #     batch's planned quantity (Requirement 2.1).
        assert record["registeredQuantity"] == planned_quantity
        assert planned_quantity == quantity

        connection = connect_database(str(db_path))
        try:
            # (B) Exactly one new batch_trace_records row was created
            #     (Requirements 2.1, 2.2).
            after_batch_records = _count(connection, "batch_trace_records")
            assert after_batch_records - before_batch_records == 1

            # (C) The single persisted record belongs to the scanned batch and
            #     carries the defaulted registered quantity (Requirement 2.2).
            row = connection.execute(
                """
                SELECT btr.registered_quantity, btr.production_batch_id
                FROM batch_trace_records btr
                JOIN production_batches pb ON pb.id = btr.production_batch_id
                WHERE pb.batch_code = ? COLLATE NOCASE
                """,
                (batch_code,),
            ).fetchall()
            assert len(row) == 1
            assert row[0]["registered_quantity"] == planned_quantity

            # (D) No per-unit machines rows were created — batch registration
            #     produces a single batch-level record, not per-unit records
            #     (Requirement 2.2).
            assert _count(connection, "machines") == before_machines

            # (E) No per-unit trace_records rows were created (Requirement 2.2).
            assert _count(connection, "trace_records") == before_trace_records
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _make_scannable_batch(
    client, usage: list[int], quantity: int, prefix: str, slack: int
) -> tuple[str, int]:
    """Generate a fresh, not-yet-registered batch and return ``(batch_code, planned)``.

    Each production batch can only be registered once, so every quantity-correction
    scenario must run against a freshly generated batch.
    """

    stock_per_batch = quantity * max(usage) + slack
    scenario = build_product_scenario(client, "ENTRYQTY", usage, stock_per_batch)
    generate = client.post(
        "/api/production-batches",
        json={
            "productModelId": scenario["product"]["id"],
            "quantity": quantity,
            "prefix": prefix,
        },
    )
    assert generate.status_code == 201, generate.get_json()
    created = generate.get_json()["data"]
    return created["batchCode"], int(created["plannedQuantity"])


def _invalid_corrections(planned_quantity: int) -> st.SearchStrategy[object]:
    """Quantity corrections outside the accepted ``1..planned_quantity`` integer range.

    Covers 0, negatives, values greater than the planned quantity, non-integer
    floats, non-numeric strings and booleans. Omitted-value forms (``None`` /
    empty / whitespace strings) are excluded on purpose because the endpoint
    treats those as "no correction" (defaults to the planned quantity) rather
    than as an invalid correction.
    """

    return st.one_of(
        st.just(0),
        st.integers(max_value=-1),
        st.integers(min_value=planned_quantity + 1),
        st.floats(allow_nan=False, allow_infinity=False).filter(
            lambda value: not float(value).is_integer()
        ),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6),
        st.booleans(),
    )


# Feature: batch-traceability, Property 6: 登记台数修正的取值边界
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=500),
    data=st.data(),
)
def test_valid_quantity_correction_takes_effect(
    usage: list[int], quantity: int, prefix: str, slack: int, data
) -> None:
    """``1..planned_quantity`` 修正生效：registered_quantity == 修正值，恰新增一条记录。

    Validates: Requirements 2.5, 2.6
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-entry-qty-ok-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        batch_code, planned_quantity = _make_scannable_batch(
            client, usage, quantity, prefix, slack
        )

        # A correction anywhere inside the inclusive 1..planned_quantity range.
        correction = data.draw(st.integers(min_value=1, max_value=planned_quantity))

        connection = connect_database(str(db_path))
        try:
            before_batch_records = _count(connection, "batch_trace_records")
        finally:
            connection.close()

        response = client.post(
            "/api/batch-entry/scan",
            json={"code": batch_code, "quantity": correction},
        )

        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["ok"] is True
        # (A) The valid correction takes effect (Requirement 2.5).
        assert body["data"]["registeredQuantity"] == correction

        connection = connect_database(str(db_path))
        try:
            # (B) Exactly one new record was created for this batch.
            after_batch_records = _count(connection, "batch_trace_records")
            assert after_batch_records - before_batch_records == 1

            row = connection.execute(
                """
                SELECT btr.registered_quantity
                FROM batch_trace_records btr
                JOIN production_batches pb ON pb.id = btr.production_batch_id
                WHERE pb.batch_code = ? COLLATE NOCASE
                """,
                (batch_code,),
            ).fetchall()
            assert len(row) == 1
            # (C) The persisted quantity equals the corrected value.
            assert row[0]["registered_quantity"] == correction
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 6: 登记台数修正的取值边界
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=500),
    data=st.data(),
)
def test_invalid_quantity_correction_rejected_without_record(
    usage: list[int], quantity: int, prefix: str, slack: int, data
) -> None:
    """非整数 / 越界修正被拒绝（400），不创建或更新任何批次登记记录。

    Validates: Requirements 2.5, 2.6
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-entry-qty-bad-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        batch_code, planned_quantity = _make_scannable_batch(
            client, usage, quantity, prefix, slack
        )

        # A correction outside the accepted 1..planned_quantity integer range.
        correction = data.draw(_invalid_corrections(planned_quantity))

        connection = connect_database(str(db_path))
        try:
            before_batch_records = _count(connection, "batch_trace_records")
        finally:
            connection.close()

        response = client.post(
            "/api/batch-entry/scan",
            json={"code": batch_code, "quantity": correction},
        )

        # (A) The out-of-range / non-integer correction is rejected with a
        #     descriptive 400 error (Requirement 2.6).
        assert response.status_code == 400, response.get_json()
        body = response.get_json()
        assert body["ok"] is False
        assert body["message"]

        connection = connect_database(str(db_path))
        try:
            # (B) No batch_trace_records row was created for the batch, and the
            #     global record count is unchanged (Requirement 2.6).
            after_batch_records = _count(connection, "batch_trace_records")
            assert after_batch_records == before_batch_records

            row = connection.execute(
                """
                SELECT 1
                FROM batch_trace_records btr
                JOIN production_batches pb ON pb.id = btr.production_batch_id
                WHERE pb.batch_code = ? COLLATE NOCASE
                """,
                (batch_code,),
            ).fetchall()
            assert row == []
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _batch_record_snapshot(connection, batch_code: str) -> list[dict[str, object]]:
    """Return every ``batch_trace_records`` row (all columns) for ``batch_code``.

    Used to prove a rejected duplicate registration leaves the existing record's
    content byte-for-byte unchanged (Requirement 2.4).
    """

    rows = connection.execute(
        """
        SELECT btr.*
        FROM batch_trace_records btr
        JOIN production_batches pb ON pb.id = btr.production_batch_id
        WHERE pb.batch_code = ? COLLATE NOCASE
        ORDER BY btr.id
        """,
        (batch_code,),
    ).fetchall()
    return [dict(row) for row in rows]


# Feature: batch-traceability, Property 7: 重复登记幂等拒绝
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_duplicate_registration_is_idempotently_rejected(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """已登记批次再次登记被拒（409），记录数保持 1、内容不变。

    Validates: Requirements 2.4
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-entry-dup-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        batch_code, planned_quantity = _make_scannable_batch(
            client, usage, quantity, prefix, slack
        )

        # First scan registers the whole batch successfully (Requirement 2.1).
        first = client.post("/api/batch-entry/scan", json={"code": batch_code})
        assert first.status_code == 201, first.get_json()

        # Snapshot the persisted record (all columns) and the global record
        # count right after the successful first registration.
        connection = connect_database(str(db_path))
        try:
            before_snapshot = _batch_record_snapshot(connection, batch_code)
            before_total = _count(connection, "batch_trace_records")
        finally:
            connection.close()
        # Exactly one record exists for the batch after the first scan.
        assert len(before_snapshot) == 1

        # Second scan of the same, already-registered batch.
        second = client.post("/api/batch-entry/scan", json={"code": batch_code})

        # (A) The duplicate registration is rejected with a descriptive 409
        #     error (Requirement 2.4).
        assert second.status_code == 409, second.get_json()
        body = second.get_json()
        assert body["ok"] is False
        assert body["message"]

        connection = connect_database(str(db_path))
        try:
            after_snapshot = _batch_record_snapshot(connection, batch_code)
            after_total = _count(connection, "batch_trace_records")
        finally:
            connection.close()

        # (B) The batch still has exactly one registration record — no second
        #     record was created (Requirement 2.4).
        assert len(after_snapshot) == 1

        # (C) The global batch_trace_records count is unchanged by the rejected
        #     duplicate.
        assert after_total == before_total

        # (D) The existing record's content is byte-for-byte unchanged — the
        #     rejected duplicate neither replaced nor mutated it (Requirement
        #     2.4).
        assert after_snapshot == before_snapshot
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 18: 登记记录质量状态初始化为待检
def test_registration_quality_status_initializes_to_assembled(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "entry-status.db")})
    client = app.test_client()
    batch_code, _planned = _make_scannable_batch(client, [1], 3, "ENTRYSTATUS", 0)
    response = client.post("/api/batch-entry/scan", json={"code": batch_code})
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["qualityStatus"] == "ASSEMBLED"
    database = connect_database(str(tmp_path / "entry-status.db"))
    try:
        assert database.execute(
            "SELECT quality_status FROM batch_trace_records"
        ).fetchone()[0] == "ASSEMBLED"
    finally:
        database.close()
