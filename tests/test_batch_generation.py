"""Property-based tests for batch generation structural invariants.

These tests drive the ``POST /api/production-batches`` endpoint (app.py, task
6.1) and assert the structural invariant of a successful batch generation:
generating one production batch adds exactly one ``production_batches`` row with
one globally-unique ``batch_code`` value, and — because a whole batch is
represented by a single code — it creates **no** per-unit ``machines`` rows and
**no** per-unit ``product_code_sets`` rows (Requirements 1.1, 1.2, 11.1, 11.2).

The scenario (product + supplier stock + per-unit usage) is assembled via the
admin API using the shared builders in ``tests/batch_strategies.py`` following
the ``tests/test_batch_inventory.py`` conventions for app / DB setup.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from traceability.codes import normalize_entity_code
from traceability.db import connect_database

from batch_strategies import (
    build_product_scenario,
    entity_code_prefixes,
    invalid_prefixes,
    invalid_quantities,
    non_integer_quantities,
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


def _batch_codes(connection) -> list[str]:
    rows = connection.execute("SELECT batch_code FROM production_batches").fetchall()
    return [row["batch_code"] for row in rows]


# Feature: batch-traceability, Property 1: 批次生成的结构不变式
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
def test_batch_generation_structural_invariant(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """恰新增一个批次行、一个全局唯一码值，且不新增逐台 machines / product_code_sets。

    Validates: Requirements 1.1, 1.2, 11.1, 11.2
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-gen-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed each supplier batch with enough stock for the worst-case part so
        # generation succeeds (this property covers the success path).
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "GEN", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        connection = connect_database(str(db_path))
        try:
            before_batches = _count(connection, "production_batches")
            before_machines = _count(connection, "machines")
            before_code_sets = _count(connection, "product_code_sets")
            before_codes = set(_batch_codes(connection))
        finally:
            connection.close()

        response = client.post(
            "/api/production-batches",
            json={
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": prefix,
            },
        )

        # Generation succeeds and returns the created batch (Requirement 1.1).
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["ok"] is True
        created = body["data"]
        created_code = created["batchCode"]

        connection = connect_database(str(db_path))
        try:
            # (A) Exactly one new production_batches row was created
            #     (Requirements 1.1, 11.1).
            after_batches = _count(connection, "production_batches")
            assert after_batches - before_batches == 1

            # (B) Exactly one new batch code value, and it is globally unique
            #     across all production batches (Requirements 1.2, 11.1).
            all_codes = _batch_codes(connection)
            new_codes = set(all_codes) - before_codes
            assert new_codes == {created_code}
            assert len(all_codes) == len(set(all_codes)), "batch codes must be unique"

            # (C) No per-unit machines rows were created — a whole batch is
            #     represented by a single code, not per-unit codes
            #     (Requirements 11.1, 11.2).
            assert _count(connection, "machines") == before_machines

            # (D) No per-unit product_code_sets rows were created
            #     (Requirements 11.1, 11.2).
            assert _count(connection, "product_code_sets") == before_code_sets
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 2: 生成字段保真
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
def test_batch_generation_field_fidelity(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """还原产品型号、前缀、planned_quantity，携带非空生成时间与操作人。

    A successfully generated batch must faithfully reflect the submitted product
    model, prefix and planned quantity (with ``planned_quantity == quantity``),
    and must carry a non-empty generation timestamp and operator.

    Validates: Requirements 1.3, 1.5
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-gen-fields-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed enough stock for the worst-case part so generation succeeds
        # (this property covers the success path / field fidelity).
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "FID", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        response = client.post(
            "/api/production-batches",
            json={
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": prefix,
            },
        )

        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["ok"] is True
        created = body["data"]
        batch_id = created["id"]

        # (A) The planned quantity is recorded as the batch's 台数, equal to the
        #     submitted quantity (Requirement 1.3).
        assert created["plannedQuantity"] == quantity

        # (B) The product model is faithfully restored (Requirement 1.5).
        assert created["productModelId"] == product_model_id

        # (C) The prefix is recorded (normalized via ``normalize_entity_code``;
        #     pattern inputs are already upper-case so it round-trips)
        #     (Requirement 1.5).
        assert created["prefix"] == normalize_entity_code(prefix, "前缀")

        # (D) A non-empty generation timestamp is carried (Requirement 1.5).
        assert isinstance(created["generatedAt"], str)
        assert created["generatedAt"].strip() != ""

        # (E) A non-empty operator is carried (Requirement 1.5).
        assert isinstance(created["generatedBy"], str)
        assert created["generatedBy"].strip() != ""

        # (F) The persisted row matches what the create response reported: read
        #     the batch back and re-assert field fidelity against the database.
        connection = connect_database(str(db_path))
        try:
            row = connection.execute(
                "SELECT product_model_id, prefix, planned_quantity, "
                "generated_by, generated_at FROM production_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        finally:
            connection.close()

        assert row is not None
        assert row["product_model_id"] == product_model_id
        assert row["prefix"] == normalize_entity_code(prefix, "前缀")
        assert row["planned_quantity"] == quantity
        assert (row["generated_by"] or "").strip() != ""
        assert (row["generated_at"] or "").strip() != ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 3 helpers: reject-and-no-side-effect on invalid generation requests
# ---------------------------------------------------------------------------


def _all_balances(connection) -> dict[int, int]:
    """Return ``{id: quantity_available}`` for every supplier inventory batch."""

    rows = connection.execute(
        "SELECT id, quantity_available FROM supplier_inventory_batches"
    ).fetchall()
    return {row["id"]: row["quantity_available"] for row in rows}


def _would_reject_quantity(value: object) -> bool:
    """Mirror the endpoint's quantity validation: True when it rejects ``value``.

    ``POST /api/production-batches`` accepts a quantity only when it is (or
    coerces to) an integer in ``[1, 999999]``. It leniently coerces numeric
    strings and integral floats, so we filter the shared invalid-quantity
    strategies to values the endpoint genuinely rejects — keeping this property
    (which asserts rejection) correct and non-flaky (Requirement 1.4).
    """

    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, float) and not value.is_integer():
        return True
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return True
    return not (1 <= quantity <= 999999)


def _rejected_quantities() -> st.SearchStrategy[object]:
    """Out-of-range (越界) and non-integer (非整数) quantities that are rejected.

    Combines the shared ``invalid_quantities`` and ``non_integer_quantities``
    strategies and filters to values the endpoint genuinely rejects.
    """

    return st.one_of(invalid_quantities(), non_integer_quantities()).filter(
        _would_reject_quantity
    )


@st.composite
def _invalid_generation_specs(draw) -> dict:
    """Draw an invalid batch-generation request across the three reject cases.

    * ``bad_quantity``    — 台数越界 / 非整数 (Requirement 1.4)
    * ``missing_model``   — 缺产品型号 (Requirement 1.7)
    * ``bad_prefix``      — 缺 / 非法前缀 (Requirement 1.7)

    Fields other than the mutated one are drawn valid so exactly one dimension
    triggers the rejection.
    """

    kind = draw(st.sampled_from(["bad_quantity", "missing_model", "bad_prefix"]))
    valid_quantity = draw(_bounded_quantities())
    valid_prefix = draw(entity_code_prefixes())

    if kind == "bad_quantity":
        return {
            "kind": kind,
            "quantity": draw(_rejected_quantities()),
            "prefix": valid_prefix,
            "include_model": True,
        }
    if kind == "missing_model":
        return {
            "kind": kind,
            "quantity": valid_quantity,
            "prefix": valid_prefix,
            "include_model": False,
        }
    # bad_prefix
    return {
        "kind": kind,
        "quantity": valid_quantity,
        "prefix": draw(invalid_prefixes()),
        "include_model": True,
    }


# Feature: batch-traceability, Property 3: 生成校验拒绝且无副作用
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    spec=_invalid_generation_specs(),
)
def test_batch_generation_validation_rejects_without_side_effects(
    usage: list[int], spec: dict
) -> None:
    """台数越界 / 非整数 / 缺产品型号或前缀被拒，计数与结存不变。

    An invalid batch-generation request (out-of-range or non-integer quantity,
    or a missing product model or prefix) is rejected with a descriptive error
    and leaves no side effects: the ``production_batches`` count is unchanged,
    every supplier inventory balance is unchanged, and no new ``batch_code``
    value exists (Requirements 1.4, 1.7).

    Validates: Requirements 1.4, 1.7
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-gen-reject-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # A valid product scenario must exist so the *only* reason the request
        # is rejected is the mutated field under test. Stock amount is
        # irrelevant here because every request is rejected before any
        # deduction; seed a modest positive balance to satisfy creation.
        scenario = build_product_scenario(client, "REJ", usage, 1000)
        product_model_id = scenario["product"]["id"]

        connection = connect_database(str(db_path))
        try:
            before_batches = _count(connection, "production_batches")
            before_movements = _count(connection, "supplier_inventory_movements")
            before_balances = _all_balances(connection)
            before_codes = set(_batch_codes(connection))
        finally:
            connection.close()

        payload: dict = {"quantity": spec["quantity"], "prefix": spec["prefix"]}
        if spec["include_model"]:
            payload["productModelId"] = product_model_id

        response = client.post("/api/production-batches", json=payload)

        # (A) The request is rejected with a descriptive client error, not
        #     created (Requirements 1.4, 1.7).
        assert response.status_code == 400, response.get_json()
        body = response.get_json()
        assert body["ok"] is False
        assert isinstance(body["message"], str) and body["message"].strip() != ""

        connection = connect_database(str(db_path))
        try:
            # (B) No production batch was created — count unchanged.
            assert _count(connection, "production_batches") == before_batches

            # (C) No batch code value was minted.
            assert set(_batch_codes(connection)) == before_codes

            # (D) Every supplier inventory balance is unchanged (no stock
            #     deducted) and no ISSUE movement was written.
            assert _all_balances(connection) == before_balances
            assert (
                _count(connection, "supplier_inventory_movements")
                == before_movements
            )
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ===========================================================================
# Task 6.12 — example-based unit tests for generation boundary / conflict cases
# ---------------------------------------------------------------------------
# These are plain (non-property) unit tests covering three specific edge cases
# on the ``POST /api/production-batches`` generation path and the batch
# download / detail endpoints:
#
#   * Requirement 1.9  — a duplicate batch_code collision is rejected with a
#                        descriptive error, creating no batch and no QR.
#   * Requirement 1.8  — downloading / viewing a non-existent batch returns 404
#                        with a descriptive error.
#   * Requirement 11.4 — re-generating a *different* code for a batch that
#                        already has a code is rejected. Per the design this is
#                        structurally impossible through the POST endpoint (each
#                        POST mints a fresh code for a NEW batch and never
#                        re-codes an existing one), so it is verified here at the
#                        code / data level: an existing batch's code is never
#                        mutated and no route exists to re-code a batch.
# ===========================================================================

import app as app_module


def _new_client(db_path: Path):
    """Create a fresh TESTING app + client backed by ``db_path``."""

    app = create_app({"TESTING": True, "DATABASE": str(db_path)})
    return app, app.test_client()


def test_duplicate_batch_code_collision_is_rejected(monkeypatch) -> None:
    """A new batch code that collides with an existing one is rejected (1.9).

    We force ``new_batch_code`` to always return the same constant so the second
    generation collides with the first on the ``production_batches.batch_code``
    UNIQUE constraint. The endpoint must reject the colliding request with a
    descriptive error, create no second batch and mint no new code.

    Validates: Requirements 1.9
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-gen-dup-"))
    db_path = workdir / "traceability.db"
    try:
        app, client = _new_client(db_path)

        usage = [1]
        # Enough stock for two generations of quantity 3.
        scenario = build_product_scenario(client, "DUP", usage, stock_per_batch=100)
        product_model_id = scenario["product"]["id"]

        # Force every generation to mint the SAME batch code value so the second
        # attempt collides on the UNIQUE constraint (Requirement 1.9).
        monkeypatch.setattr(
            app_module, "new_batch_code", lambda now_compact, prefix: "B-DUP-FIXED-CODE"
        )

        first = client.post(
            "/api/production-batches",
            json={"productModelId": product_model_id, "quantity": 3, "prefix": "DUP"},
        )
        assert first.status_code == 201, first.get_json()

        connection = connect_database(str(db_path))
        try:
            before_batches = _count(connection, "production_batches")
            before_movements = _count(connection, "supplier_inventory_movements")
            before_balances = _all_balances(connection)
            before_codes = set(_batch_codes(connection))
        finally:
            connection.close()

        # Second generation mints the same code value -> collision.
        second = client.post(
            "/api/production-batches",
            json={"productModelId": product_model_id, "quantity": 3, "prefix": "DUP"},
        )

        # (A) Rejected with a descriptive conflict error (Requirement 1.9).
        assert second.status_code == 409, second.get_json()
        body = second.get_json()
        assert body["ok"] is False
        assert isinstance(body["message"], str) and body["message"].strip() != ""

        # (B) No second batch was created, no new code minted, and no stock was
        #     deducted / no ISSUE movement written (whole thing rolled back).
        connection = connect_database(str(db_path))
        try:
            assert _count(connection, "production_batches") == before_batches
            assert set(_batch_codes(connection)) == before_codes
            assert _all_balances(connection) == before_balances
            assert (
                _count(connection, "supplier_inventory_movements") == before_movements
            )
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_download_nonexistent_batch_returns_404() -> None:
    """Downloading / viewing a non-existent batch returns a descriptive 404 (1.8).

    Both the QR download endpoint and the batch detail endpoint must reject a
    request for a batch id that does not exist with a 404 and a descriptive
    error message.

    Validates: Requirements 1.8
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-gen-404-"))
    db_path = workdir / "traceability.db"
    try:
        _app, client = _new_client(db_path)

        missing_id = 999_999

        qr_response = client.get(f"/api/production-batches/{missing_id}/qr")
        assert qr_response.status_code == 404, qr_response.get_json()
        qr_body = qr_response.get_json()
        assert qr_body["ok"] is False
        assert isinstance(qr_body["message"], str) and qr_body["message"].strip() != ""

        detail_response = client.get(f"/api/production-batches/{missing_id}")
        assert detail_response.status_code == 404, detail_response.get_json()
        detail_body = detail_response.get_json()
        assert detail_body["ok"] is False
        assert (
            isinstance(detail_body["message"], str)
            and detail_body["message"].strip() != ""
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_existing_coded_batch_is_never_recoded_with_a_different_code() -> None:
    """Re-generating a different code for an already-coded batch is prevented (11.4).

    Requirement 11.4 forbids assigning a *different* batch code to a production
    batch that already has one. Per the design this is structurally impossible
    through the API: every ``POST /api/production-batches`` mints a fresh code
    for a NEW batch and there is no route that re-codes an existing batch. This
    test verifies that structural prevention at the code / data level:

    * generating twice for the same product yields two *distinct* batches with
      two *distinct* codes (a second generation never re-codes the first);
    * the first batch's code is unchanged after the second generation;
    * ``production_batches.batch_code`` is UNIQUE (one batch <-> one code); and
    * no PUT / PATCH / mutating route exists on ``/api/production-batches/<id>``
      that could assign a different code to an existing batch.

    Validates: Requirements 11.4
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-gen-recode-"))
    db_path = workdir / "traceability.db"
    try:
        app, client = _new_client(db_path)

        usage = [1]
        scenario = build_product_scenario(client, "RECODE", usage, stock_per_batch=100)
        product_model_id = scenario["product"]["id"]

        first = client.post(
            "/api/production-batches",
            json={"productModelId": product_model_id, "quantity": 2, "prefix": "RC"},
        )
        assert first.status_code == 201, first.get_json()
        first_batch = first.get_json()["data"]
        first_id = first_batch["id"]
        first_code = first_batch["batchCode"]

        second = client.post(
            "/api/production-batches",
            json={"productModelId": product_model_id, "quantity": 2, "prefix": "RC"},
        )
        assert second.status_code == 201, second.get_json()
        second_batch = second.get_json()["data"]
        second_id = second_batch["id"]
        second_code = second_batch["batchCode"]

        # (A) A second generation creates a NEW batch with a NEW, distinct code —
        #     it never re-codes the existing batch (Requirement 11.4).
        assert second_id != first_id
        assert second_code != first_code

        connection = connect_database(str(db_path))
        try:
            # (B) The first batch's code is unchanged after the second
            #     generation (it was never re-coded to a different value).
            first_row = connection.execute(
                "SELECT batch_code FROM production_batches WHERE id = ?",
                (first_id,),
            ).fetchone()
            assert first_row["batch_code"] == first_code

            # (C) One batch <-> one code: batch_code is UNIQUE at the schema
            #     level, so a batch cannot hold two different codes.
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'production_batches'"
            ).fetchone()["sql"]
            assert "batch_code" in table_sql
            assert "UNIQUE" in table_sql.upper()
        finally:
            connection.close()

        # (D) No mutating route exists that could re-code an existing batch:
        #     the only rules on ``/api/production-batches/<id>`` are read-only
        #     (GET) endpoints, and re-generation is always a POST to the
        #     collection that mints a fresh code for a new batch.
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith("/api/production-batches/") and "<" in rule.rule:
                mutating = {"PUT", "PATCH", "DELETE"} & set(rule.methods)
                assert not mutating, (
                    f"unexpected mutating route {sorted(mutating)} on {rule.rule} "
                    "could allow re-coding an existing batch"
                )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
