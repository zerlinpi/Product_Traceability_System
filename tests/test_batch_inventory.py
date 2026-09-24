"""Property-based tests for batch inventory deduction (Property 11).

These tests exercise the ``deduct_batch_inventory`` helper (traceability/inventory.py, task 5.1)
which, within an already-open ``BEGIN IMMEDIATE`` transaction, deducts
``N × 每套用量`` (per-unit usage) from each supplier inventory batch bound by the
product's ACTIVE trace plan and writes exactly one ``ISSUE`` movement per
deduction (Requirement 4.1).

The production-batch generation endpoint (``POST /api/production-batches``,
task 6.1) is implemented separately and may not yet exist, so these tests drive
the helper directly: they build a realistic product + supplier-stock scenario
via the admin API, open a transaction on a direct connection, insert a
``production_batches`` row, then call ``deduct_batch_inventory`` and assert the
resulting balances and movements.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from traceability.api import production_batches as batches_module
from traceability.db import connect_database
from traceability.inventory import (
    compute_batch_inventory_requirements,
    deduct_batch_inventory,
)

from batch_strategies import build_product_scenario, per_unit_usages, valid_quantities


# Supplier inventory batches accept at most 10,000,000 units on creation, so we
# keep planned quantities within a range whose worst-case deduction
# (``quantity × max(usage)``) plus slack stays under that cap.
_MAX_PLANNED_QUANTITY = 2_000_000


def _bounded_quantities() -> st.SearchStrategy[int]:
    """Valid planned quantities kept below the supplier-stock creation cap."""

    return valid_quantities().filter(lambda value: value <= _MAX_PLANNED_QUANTITY)


def _balances(connection, batch_ids: list[int]) -> dict[int, int]:
    """Return the current ``quantity_available`` for each supplier batch id."""

    balances: dict[int, int] = {}
    for batch_id in batch_ids:
        row = connection.execute(
            "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        balances[batch_id] = row["quantity_available"]
    return balances


# Feature: batch-traceability, Property 11: 库存扣减按整批用量并写领用流水
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_inventory_deduction_matches_batch_usage_and_writes_issue(
    usage: list[int], quantity: int, slack: int
) -> None:
    """结存下降量 = N × 每套用量，且每次扣减恰一条 -(N×每套用量) 的 ISSUE 流水。

    Validates: Requirements 4.1
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-inv-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed each supplier batch with enough stock for the worst-case part so
        # the whole deduction succeeds (Property 11 is the success path).
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "INV", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]
        batch_ids = [inventory["id"] for inventory in scenario["inventory_batches"]]

        connection = connect_database(str(db_path))
        try:
            plan_row = connection.execute(
                """
                SELECT id FROM trace_plans
                WHERE product_model_id = ? AND status = 'ACTIVE'
                ORDER BY activated_at DESC, id DESC LIMIT 1
                """,
                (product_model_id,),
            ).fetchone()
            assert plan_row is not None, "product should have an ACTIVE trace plan"
            trace_plan_id = plan_row["id"]

            # Expected per-supplier-batch per-unit usage and required deduction.
            requirements = {
                requirement["supplier_inventory_batch_id"]: requirement
                for requirement in compute_batch_inventory_requirements(
                    connection, trace_plan_id, quantity
                )
            }
            # Every seeded supplier batch is bound at least once, so all appear.
            assert set(requirements) == set(batch_ids)

            before_balances = _balances(connection, batch_ids)
            before_movements = connection.execute(
                "SELECT COUNT(*) AS n FROM supplier_inventory_movements"
            ).fetchone()["n"]

            timestamp = "2026-01-01T00:00:00+00:00"
            batch_code = "B-INV-TESTBATCH"

            # The helper requires an already-open transaction (Requirement 4.5);
            # the production_batches row satisfies the movement FK.
            connection.execute("BEGIN IMMEDIATE")
            try:
                production_batch_id = connection.execute(
                    """
                    INSERT INTO production_batches(
                        batch_code, product_model_id, trace_plan_id, prefix,
                        planned_quantity, generated_by, generated_by_user_id,
                        generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_code, product_model_id, trace_plan_id, "INV",
                        quantity, "tester", None, timestamp,
                    ),
                ).lastrowid
                consumption = deduct_batch_inventory(
                    connection,
                    trace_plan_id=trace_plan_id,
                    product_model_id=product_model_id,
                    quantity=quantity,
                    production_batch_id=production_batch_id,
                    batch_code=batch_code,
                    actor_user_id=None,
                    timestamp=timestamp,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

            after_balances = _balances(connection, batch_ids)

            # (A) Each supplier batch balance drops by exactly N × 每套用量.
            for batch_id in batch_ids:
                per_unit = requirements[batch_id]["per_unit_usage"]
                expected_drop = quantity * per_unit
                actual_drop = before_balances[batch_id] - after_balances[batch_id]
                assert actual_drop == expected_drop, (
                    f"batch {batch_id}: expected drop {expected_drop}, got {actual_drop}"
                )

            # (B) Exactly one ISSUE movement is written per deduction.
            after_movements = connection.execute(
                "SELECT COUNT(*) AS n FROM supplier_inventory_movements"
            ).fetchone()["n"]
            assert after_movements - before_movements == len(batch_ids)

            for batch_id in batch_ids:
                per_unit = requirements[batch_id]["per_unit_usage"]
                expected_change = -(quantity * per_unit)
                movements = connection.execute(
                    """
                    SELECT * FROM supplier_inventory_movements
                    WHERE production_batch_id = ? AND inventory_batch_id = ?
                    """,
                    (production_batch_id, batch_id),
                ).fetchall()
                assert len(movements) == 1, (
                    f"batch {batch_id}: expected exactly one ISSUE movement, "
                    f"got {len(movements)}"
                )
                movement = movements[0]
                assert movement["movement_type"] == "ISSUE"
                assert movement["quantity_change"] == expected_change
                assert movement["balance_after"] == after_balances[batch_id]

            # (C) The consumption records returned mirror the deductions.
            consumption_by_batch = {
                record["supplier_inventory_batch_id"]: record
                for record in consumption
            }
            assert set(consumption_by_batch) == set(batch_ids)
            for batch_id in batch_ids:
                per_unit = requirements[batch_id]["per_unit_usage"]
                assert (
                    consumption_by_batch[batch_id]["quantity_consumed"]
                    == quantity * per_unit
                )
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@st.composite
def _insufficient_params(draw) -> tuple[list[int], int, int]:
    """Draw (usage, quantity, stock_per_batch) that guarantees a shortfall.

    The batch bound by the part with the greatest per-unit usage requires
    ``quantity × max(usage)`` units. Seeding every supplier batch with strictly
    fewer units than that guarantees at least one part is short, so the whole
    generation must fail (Requirement 4.2). Stock stays >= 1 to satisfy the
    supplier-inventory-batch creation bound (1..10,000,000).
    """

    usage = draw(per_unit_usages())
    quantity = draw(_bounded_quantities())
    required_max = quantity * max(usage)
    if required_max < 2:
        # required_max == 1 (quantity == 1 and max(usage) == 1) leaves no room
        # for an insufficient-but-positive stock; bump quantity so a shortfall
        # with stock >= 1 exists.
        quantity = 2
        required_max = quantity * max(usage)
    shortfall = draw(st.integers(min_value=1, max_value=required_max - 1))
    stock_per_batch = required_max - shortfall  # in [1, required_max - 1]
    return usage, quantity, stock_per_batch


def _all_balances(connection) -> dict[int, int]:
    """Return ``{id: quantity_available}`` for every supplier inventory batch."""

    rows = connection.execute(
        "SELECT id, quantity_available FROM supplier_inventory_batches"
    ).fetchall()
    return {row["id"]: row["quantity_available"] for row in rows}


def _count(connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _batch_codes(connection) -> set[str]:
    rows = connection.execute("SELECT batch_code FROM production_batches").fetchall()
    return {row["batch_code"] for row in rows}


# Feature: batch-traceability, Property 12: 结存不足整次失败且无副作用
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(params=_insufficient_params())
def test_insufficient_stock_fails_whole_batch_without_side_effects(
    params: tuple[list[int], int, int],
) -> None:
    """结存不足使整次生成失败，且结存 / movements / production_batches / 批次码值均不变。

    Validates: Requirements 4.2
    """

    usage, quantity, stock_per_batch = params

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-insuf-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed a product whose bound supplier batches each hold fewer units than
        # the worst-case part needs, guaranteeing a shortfall.
        scenario = build_product_scenario(client, "INS", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        connection = connect_database(str(db_path))
        try:
            before_balances = _all_balances(connection)
            before_movements = _count(connection, "supplier_inventory_movements")
            before_batches = _count(connection, "production_batches")
            before_consumption = _count(
                connection, "production_batch_supplier_consumption"
            )
            before_codes = _batch_codes(connection)

            # Generate via the endpoint, which wraps deduction in a transaction
            # that must roll back entirely on a shortfall (Requirements 4.2, 4.4).
            response = client.post(
                "/api/production-batches",
                json={
                    "productModelId": product_model_id,
                    "quantity": quantity,
                    "prefix": "INS",
                },
            )

            # The whole generation is rejected as insufficient stock (409).
            assert response.status_code == 409, response.get_json()
            body = response.get_json()
            assert body["ok"] is False
            assert "库存不足" in body["message"]

            # (A) Every supplier balance is unchanged.
            assert _all_balances(connection) == before_balances

            # (B) No ISSUE movement was written.
            assert _count(connection, "supplier_inventory_movements") == before_movements

            # (C) No production batch (and no consumption row) was created.
            assert _count(connection, "production_batches") == before_batches
            assert (
                _count(connection, "production_batch_supplier_consumption")
                == before_consumption
            )

            # (D) No new batch code value exists.
            assert _batch_codes(connection) == before_codes
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _table_snapshot(connection, table: str) -> list[tuple]:
    """Return every row of ``table`` as a list of tuples, ordered by rowid.

    Rows are captured as plain tuples so two snapshots can be compared
    byte-for-byte (field-by-field) with ``==`` regardless of ``sqlite3.Row``
    identity.
    """

    rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return [tuple(row) for row in rows]


def _full_snapshot(connection) -> dict[str, object]:
    """Snapshot every table touched by batch generation for exact comparison.

    Covers supplier balances / movements (Requirement 4.1 side effects), the
    ``production_batches`` rows and their batch code values, and the
    batch-level supplier consumption associations (Requirement 5.1). A rolled
    back transaction (Requirement 4.4) must leave all of these identical to the
    pre-generation snapshot.
    """

    return {
        "supplier_inventory_batches": _table_snapshot(
            connection, "supplier_inventory_batches"
        ),
        "supplier_inventory_movements": _table_snapshot(
            connection, "supplier_inventory_movements"
        ),
        "production_batches": _table_snapshot(connection, "production_batches"),
        "production_batch_supplier_consumption": _table_snapshot(
            connection, "production_batch_supplier_consumption"
        ),
        "batch_codes": _batch_codes(connection),
    }


class _InjectedFailure(RuntimeError):
    """Sentinel error injected mid-transaction to force a rollback."""


# Feature: batch-traceability, Property 13: 事务回滚原子性
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_transaction_rollback_is_atomic_on_mid_transaction_failure(
    usage: list[int], quantity: int, slack: int
) -> None:
    """生成事务中途注入失败，回滚后库存 / 流水 / 批次 / 关联 / 码值与快照逐字段相等。

    ``POST /api/production-batches`` wraps the batch insert, inventory
    deduction + ISSUE movements, supplier-consumption inserts and the audit
    write in a single ``BEGIN IMMEDIATE`` transaction (Requirement 4.3). We
    seed enough stock that the generation would otherwise succeed, then inject
    a failure at the audit-write step — the last step, so the batch row,
    deducted balances, ISSUE movements and consumption rows have all been
    written within the open transaction. The endpoint must roll the whole
    transaction back so every affected table is byte-for-byte equal to the
    pre-generation snapshot, leaving no partial deduction, movement, batch,
    association or code value (Requirement 4.4).

    Validates: Requirements 4.3, 4.4
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-rollback-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed enough stock so the deduction itself would succeed; only the
        # injected failure aborts the transaction.
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "RBK", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        connection = connect_database(str(db_path))
        try:
            before = _full_snapshot(connection)

            # Inject a failure at the audit-write step. It runs after the batch
            # insert, inventory deduction / movements and consumption inserts,
            # so the maximum amount of partial work exists inside the open
            # transaction when the failure fires (Requirement 4.4).
            #
            # Patched where the route calls it, not on ``app``: the route moved
            # into a blueprint, and a module holds its own reference to an
            # imported name, so patching app.record_audit_event would no longer
            # reach it.
            def _boom(*_args, **_kwargs):
                raise _InjectedFailure("injected mid-transaction failure")

            with patch.object(batches_module, "record_audit_event", _boom):
                response = client.post(
                    "/api/production-batches",
                    json={
                        "productModelId": product_model_id,
                        "quantity": quantity,
                        "prefix": "RBK",
                    },
                )

            # The injected error surfaces as an unexpected 500; crucially the
            # request did not succeed, so nothing should have been committed.
            assert response.status_code == 500, response.get_json()
            body = response.get_json()
            assert body["ok"] is False

            # The whole transaction rolled back: every affected table is
            # byte-for-byte equal to the pre-generation snapshot (Requirement
            # 4.4) — balances, ISSUE movements, production batches, supplier
            # consumption associations and batch code values all unchanged.
            after = _full_snapshot(connection)
            for table, before_rows in before.items():
                assert after[table] == before_rows, (
                    f"{table} changed after a rolled-back generation: "
                    f"{before_rows!r} != {after[table]!r}"
                )
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 16: 同部件消耗量守恒
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_same_part_consumption_sum_equals_planned_quantity_times_usage(
    usage: list[int], quantity: int, slack: int
) -> None:
    """针对同一部件，各关联记录消耗数量之和 = 计划台数 × 每套用量。

    ``POST /api/production-batches`` persists one
    ``production_batch_supplier_consumption`` row per consumed supplier batch,
    each carrying its ``part_type_id`` and ``quantity_consumed`` (Requirement
    5.1). Property 16 asserts the batch-level conservation invariant: summing
    ``quantity_consumed`` across every association row for a given part equals
    ``planned_quantity × 每套用量`` for that part (Requirement 5.2).

    The design's 每套用量 口径 is "the number of ``trace_plan_slots`` in the
    active trace plan binding a supplier batch"; a part's per-unit usage is the
    total such slots that expect that part. We derive the expected per-part
    usage straight from ``trace_plan_slots`` (the source of truth) so the
    assertion is independent of the deduction helper being validated.

    Validates: Requirements 5.2
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-cons-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed enough stock for the worst-case part so generation succeeds
        # (Property 16 is the success path).
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "CONS", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        response = client.post(
            "/api/production-batches",
            json={
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": "CONS",
            },
        )
        assert response.status_code == 201, response.get_json()
        production_batch_id = response.get_json()["data"]["id"]

        connection = connect_database(str(db_path))
        try:
            plan_row = connection.execute(
                """
                SELECT id FROM trace_plans
                WHERE product_model_id = ? AND status = 'ACTIVE'
                ORDER BY activated_at DESC, id DESC LIMIT 1
                """,
                (product_model_id,),
            ).fetchone()
            assert plan_row is not None, "product should have an ACTIVE trace plan"
            trace_plan_id = plan_row["id"]

            # Expected per-part 每套用量 = number of active-plan slots binding a
            # supplier batch, aggregated by the part the slot expects.
            expected_usage_rows = connection.execute(
                """
                SELECT part_type_id, COUNT(*) AS slots
                FROM trace_plan_slots
                WHERE trace_plan_id = ? AND supplier_inventory_batch_id IS NOT NULL
                GROUP BY part_type_id
                """,
                (trace_plan_id,),
            ).fetchall()
            expected_usage_by_part = {
                row["part_type_id"]: row["slots"] for row in expected_usage_rows
            }
            assert expected_usage_by_part, "product should bind at least one part"

            # Actual consumed totals for this batch, aggregated per part.
            actual_rows = connection.execute(
                """
                SELECT part_type_id, SUM(quantity_consumed) AS total
                FROM production_batch_supplier_consumption
                WHERE production_batch_id = ?
                GROUP BY part_type_id
                """,
                (production_batch_id,),
            ).fetchall()
            actual_consumed_by_part = {
                row["part_type_id"]: row["total"] for row in actual_rows
            }

            # The parts that consumed stock are exactly those the plan binds.
            assert set(actual_consumed_by_part) == set(expected_usage_by_part)

            # Conservation: per part, the consumed sum equals N × 每套用量.
            for part_type_id, per_unit in expected_usage_by_part.items():
                expected_total = quantity * per_unit
                assert actual_consumed_by_part[part_type_id] == expected_total, (
                    f"part {part_type_id}: expected consumed sum {expected_total}, "
                    f"got {actual_consumed_by_part[part_type_id]}"
                )
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
