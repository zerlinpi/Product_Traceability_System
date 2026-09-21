"""Property-based test for concurrent batch-generation serialization (Property 14).

Requirement 4.5: WHILE multiple concurrent batch-generation requests consume the
same supplier inventory batch, THE BatchCodeGenerator SHALL serialize deductions
against that batch's available balance so each request's cumulative deduction
never exceeds the available balance.

The production-batch endpoint (``POST /api/production-batches``) wraps its
inventory deduction in a ``BEGIN IMMEDIATE`` transaction; combined with SQLite's
WAL journal mode and the 10s ``busy_timeout`` configured in
``traceability/db.py`` (``connect_database`` / ``initialize_database``),
concurrent writers block and serialize rather than oversell. This test drives
real concurrency with a ``ThreadPoolExecutor``: several workers contend for one
shared supplier inventory batch, each issuing its request through its own Flask
``test_client`` so each gets its own per-request DB connection via ``get_db``.

The scenario deliberately seeds strictly less stock than all workers together
need, so at least one request must lose the race. The invariants asserted after
the dust settles prove serialization held: cumulative deductions never exceed
the initial balance, the final balance is never negative, and the balance
reconciles exactly (no lost updates, no double spend).
"""

from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from traceability.db import connect_database

from batch_strategies import build_product_scenario


@st.composite
def _contention_scenarios(draw) -> tuple[int, int, int, int]:
    """Draw ``(workers, quantity, allowed_successes, initial_stock)``.

    A single shared supplier inventory batch is seeded with
    ``quantity * allowed_successes + remainder`` units where
    ``0 <= remainder < quantity`` and ``1 <= allowed_successes < workers``.

    Because the product uses a single component with per-unit usage 1, each
    generation deducts exactly ``quantity`` units. Serialized against the seeded
    balance, exactly ``allowed_successes`` requests can succeed and the rest
    must be rejected for insufficient stock — guaranteeing genuine contention on
    the shared balance rather than a scenario where every request trivially
    fits.
    """

    workers = draw(st.integers(min_value=2, max_value=5))
    quantity = draw(st.integers(min_value=1, max_value=100))
    allowed_successes = draw(st.integers(min_value=1, max_value=workers - 1))
    remainder = draw(st.integers(min_value=0, max_value=quantity - 1))
    initial_stock = quantity * allowed_successes + remainder
    return workers, quantity, allowed_successes, initial_stock


# Feature: batch-traceability, Property 14: 并发扣减串行化不超结存
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scenario=_contention_scenarios())
def test_concurrent_deductions_serialize_without_overselling(
    scenario: tuple[int, int, int, int],
) -> None:
    """并发扣减串行化：累计扣减不超初始结存，最终结存不为负且账目守恒。

    Validates: Requirements 4.5
    """

    workers, quantity, allowed_successes, initial_stock = scenario

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-concurrency-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})

        # Build the scenario up front on the main thread: one component with
        # per-unit usage 1 bound to a single supplier inventory batch, so every
        # concurrent generation contends for the *same* balance and each deducts
        # exactly ``quantity`` units.
        setup_client = app.test_client()
        scenario_data = build_product_scenario(
            setup_client, "CON", [1], initial_stock
        )
        product_model_id = scenario_data["product"]["id"]
        supplier_batch_id = scenario_data["inventory_batches"][0]["id"]

        def _fire(_index: int) -> tuple[int, dict]:
            # Each worker uses its own test client, so the request runs in its
            # own app/request context with its own DB connection (``get_db``),
            # exercising real concurrent writers against the shared SQLite file.
            client = app.test_client()
            response = client.post(
                "/api/production-batches",
                json={
                    "productModelId": product_model_id,
                    "quantity": quantity,
                    "prefix": "CON",
                },
            )
            return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_fire, range(workers)))

        successes = [item for item in results if item[0] == 201]
        failures = [item for item in results if item[0] != 201]

        # Every non-success must be a clean insufficient-stock rejection (409) —
        # not a crash, a deadlock, or a torn/lost write. This is what proves the
        # contention was resolved by serialization, not by errors.
        for status, body in failures:
            assert status == 409, (status, body)
            assert body is not None and body["ok"] is False
            assert "库存不足" in body["message"], body

        connection = connect_database(str(db_path))
        try:
            final_balance = connection.execute(
                "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
                (supplier_batch_id,),
            ).fetchone()["quantity_available"]

            issue_rows = connection.execute(
                """
                SELECT quantity_change FROM supplier_inventory_movements
                WHERE inventory_batch_id = ? AND movement_type = 'ISSUE'
                """,
                (supplier_batch_id,),
            ).fetchall()

            batch_count = connection.execute(
                "SELECT COUNT(*) AS n FROM production_batches"
            ).fetchone()["n"]
        finally:
            connection.close()

        cumulative_deducted = -sum(row["quantity_change"] for row in issue_rows)

        # (A) The final balance is never negative (Requirement 4.5).
        assert final_balance >= 0

        # (B) Cumulative deductions never exceed the initial available balance.
        assert cumulative_deducted <= initial_stock

        # (C) The balance reconciles exactly: no lost update, no double spend.
        assert final_balance == initial_stock - cumulative_deducted

        # (D) Each successful generation wrote exactly one ISSUE of ``quantity``.
        assert len(issue_rows) == len(successes)
        assert cumulative_deducted == len(successes) * quantity

        # (E) Serialization admits exactly ``allowed_successes`` winners; the
        # remaining workers are rejected — proving no overselling race let an
        # extra request through.
        assert len(successes) == allowed_successes
        assert len(failures) == workers - allowed_successes

        # (F) Exactly one production batch row exists per successful generation.
        assert batch_count == allowed_successes
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
