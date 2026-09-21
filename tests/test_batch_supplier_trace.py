"""Property-based tests for batch-level supplier-batch association / reverse trace.

These tests drive the ``POST /api/production-batches`` generation endpoint and
the ``GET /api/production-batches/<id>`` detail endpoint (app.py, tasks 6.1 and
7.1). They assert **Property 15: 批次级供应关联即反向追溯来源** — after a batch
is generated, the ``production_batch_supplier_consumption`` rows correspond
one-to-one with the supplier inventory batches that were actually deducted
(evidenced by the ``ISSUE`` movements written in the same transaction), and this
same set is exactly what the detail endpoint's ``reverseTrace`` list reports
(Requirements 5.1, 5.3, 10.2).

The scan-query endpoint (``POST /api/batch-trace/query``) belongs to task 12.1
and may not exist yet, so it is only cross-checked opportunistically when the
route is present; the authoritative assertions are against the consumption table
and the detail endpoint's ``reverseTrace``.

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
from traceability.db import connect_database

from batch_strategies import (
    QUANTITY_MIN,
    build_product_scenario,
    build_supplier_and_part,
    build_supplier_inventory_batch,
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


def _consumption_rows(connection, production_batch_id: int) -> list[dict]:
    """Return the batch-level consumption rows for one production batch."""

    rows = connection.execute(
        """
        SELECT supplier_inventory_batch_id, part_type_id, quantity_consumed
        FROM production_batch_supplier_consumption
        WHERE production_batch_id = ?
        """,
        (production_batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _deducted_supplier_batches(connection, production_batch_id: int) -> set[int]:
    """Supplier batch ids that received an ISSUE movement for this batch.

    These are the supplier inventory batches that were *actually deducted* in
    the generation transaction — the ground truth the consumption association
    must mirror one-to-one (Requirement 5.1).
    """

    rows = connection.execute(
        """
        SELECT DISTINCT inventory_batch_id
        FROM supplier_inventory_movements
        WHERE production_batch_id = ? AND movement_type = 'ISSUE'
        """,
        (production_batch_id,),
    ).fetchall()
    return {row["inventory_batch_id"] for row in rows}


# Feature: batch-traceability, Property 15: 批次级供应关联即反向追溯来源
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
def test_supplier_consumption_matches_deductions_and_reverse_trace(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """关联记录集合与被扣减供应批次一一对应，且与详情反向追溯清单一致。

    Validates: Requirements 5.1, 5.3, 10.2
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-suptrace-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed enough stock for the worst-case part so generation succeeds; this
        # property covers the success path (a consumed supplier batch per part).
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "TRC", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]
        seeded_batch_ids = {inv["id"] for inv in scenario["inventory_batches"]}

        response = client.post(
            "/api/production-batches",
            json={
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": prefix,
            },
        )
        assert response.status_code == 201, response.get_json()
        created = response.get_json()["data"]
        production_batch_id = created["id"]

        connection = connect_database(str(db_path))
        try:
            consumption = _consumption_rows(connection, production_batch_id)
            deducted = _deducted_supplier_batches(connection, production_batch_id)
        finally:
            connection.close()

        # (A) Every per-unit usage in the scenario is >= 1, so each seeded
        #     supplier batch is deducted exactly once.
        assert deducted == seeded_batch_ids

        # (B) The consumption association is one-to-one with the deducted
        #     supplier batches: exactly one row per deducted batch, no extras,
        #     no duplicates (Requirement 5.1).
        consumption_batch_ids = [row["supplier_inventory_batch_id"] for row in consumption]
        assert len(consumption_batch_ids) == len(set(consumption_batch_ids)), (
            "each deducted supplier batch must have exactly one association row"
        )
        assert set(consumption_batch_ids) == deducted

        # Index the consumption rows for cross-referencing with reverse trace.
        consumption_by_batch = {
            row["supplier_inventory_batch_id"]: row for row in consumption
        }
        # Consumed amount for each part equals N × 每套用量 (Requirement 5.2 lens
        # on the association: the per-batch consumed amount is exactly what the
        # deduction removed). ``usage[i]`` is the per-unit usage for that batch.
        usage_by_batch = {
            inv["id"]: usage[index]
            for index, inv in enumerate(scenario["inventory_batches"])
        }
        for batch_id, per_unit in usage_by_batch.items():
            assert consumption_by_batch[batch_id]["quantity_consumed"] == quantity * per_unit

        # (C) The detail endpoint's reverseTrace list matches the consumption
        #     association one-to-one — the reverse-trace source is exactly the
        #     set of consumed supplier batches (Requirements 5.3, 10.2).
        detail_response = client.get(f"/api/production-batches/{production_batch_id}")
        assert detail_response.status_code == 200, detail_response.get_json()
        detail = detail_response.get_json()["data"]
        reverse_trace = detail["reverseTrace"]

        rt_batch_ids = [entry["supplierInventoryBatchId"] for entry in reverse_trace]
        assert len(rt_batch_ids) == len(set(rt_batch_ids)), (
            "reverseTrace must list each supplier batch at most once"
        )
        assert set(rt_batch_ids) == deducted

        # Each reverse-trace entry carries supplier batch, part and consumed
        # amount, and agrees field-by-field with the consumption association.
        for entry in reverse_trace:
            batch_id = entry["supplierInventoryBatchId"]
            source = consumption_by_batch[batch_id]
            assert entry["partTypeId"] == source["part_type_id"]
            assert entry["quantityConsumed"] == source["quantity_consumed"]

        # (D) Opportunistic cross-check: if the scan-query endpoint (task 12.1)
        #     exists, its reverse-trace list must agree with the detail's
        #     (Requirement 10.2). It is absent until task 12.1, so a 404 / 405
        #     is tolerated and does not fail this property.
        scan_response = client.post(
            "/api/batch-trace/query", json={"code": created["batchCode"]}
        )
        if scan_response.status_code == 200:
            scan_data = scan_response.get_json()["data"]
            scan_trace = scan_data.get("reverseTrace")
            if scan_trace is not None:
                scan_batch_ids = {
                    entry["supplierInventoryBatchId"] for entry in scan_trace
                }
                assert scan_batch_ids == deducted
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Number of production batches to generate per scenario. Kept small so total
# stock stays comfortably below the supplier-batch cap of 10,000,000 units.
_MAX_BATCHES = 3
# Per-batch quantity cap so the worst-case total deduction
# (sum(quantities) × max(usage)) across up to _MAX_BATCHES stays under the cap.
_FORWARD_QUANTITY_MAX = 300_000


def _forward_quantities() -> st.SearchStrategy[list[int]]:
    """A non-empty list of valid planned quantities for several batches."""

    return st.lists(
        st.one_of(
            st.just(QUANTITY_MIN),
            st.integers(min_value=QUANTITY_MIN, max_value=_FORWARD_QUANTITY_MAX),
        ),
        min_size=1,
        max_size=_MAX_BATCHES,
    )


def _forward_trace(client, supplier_inventory_batch_id: int) -> list[dict]:
    """Fetch the forward-trace list for a supplier inventory batch."""

    response = client.get(
        f"/api/supplier-inventory-batches/{supplier_inventory_batch_id}/forward-trace"
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


# Feature: batch-traceability, Property 17: 供应批次正向追溯映射
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantities=_forward_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_supplier_forward_trace_maps_exactly_to_consuming_batches(
    usage: list[int], quantities: list[int], prefix: str, slack: int
) -> None:
    """正向追溯集合恰等于消耗过它的全部生产批次；无消耗返回空清单。

    Validates: Requirements 5.5, 5.6
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-fwdtrace-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed enough stock across every generated batch: the worst-case part
        # is deducted ``sum(quantities) × max(usage)`` units in total.
        stock_per_batch = sum(quantities) * max(usage) + slack
        scenario = build_product_scenario(client, "TRC", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]
        product_supplier_batches = scenario["inventory_batches"]
        usage_by_batch = {
            inv["id"]: usage[index]
            for index, inv in enumerate(product_supplier_batches)
        }

        # An extra standalone supplier batch that is *not* bound to any product,
        # so no production batch ever consumes it (Requirement 5.6).
        _supplier, unrelated_part = build_supplier_and_part(client, "TRC-UNCONSUMED")
        unconsumed_batch = build_supplier_inventory_batch(
            client, unrelated_part["id"], stock_per_batch + 1, "TRC-UNCONSUMED"
        )

        # Generate several production batches for the product; each generation
        # consumes every one of the product's supplier batches exactly once.
        expected_by_supplier: dict[int, dict[int, int]] = {
            inv["id"]: {} for inv in product_supplier_batches
        }
        for index, quantity in enumerate(quantities):
            response = client.post(
                "/api/production-batches",
                json={
                    "productModelId": product_model_id,
                    "quantity": quantity,
                        "prefix": f"{prefix[:31]}{index % 10}",
                },
            )
            assert response.status_code == 201, response.get_json()
            created = response.get_json()["data"]
            batch_id = created["id"]
            for supplier_batch_id, per_unit in usage_by_batch.items():
                expected_by_supplier[supplier_batch_id][batch_id] = quantity * per_unit

        # (A) For each consumed supplier batch, the forward-trace set is exactly
        #     the set of production batches that consumed it, with matching
        #     consumed amounts (Requirement 5.5).
        for supplier_batch_id, expected_map in expected_by_supplier.items():
            entries = _forward_trace(client, supplier_batch_id)
            traced_ids = [entry["productionBatchId"] for entry in entries]
            assert len(traced_ids) == len(set(traced_ids)), (
                "each consuming production batch must appear at most once"
            )
            assert set(traced_ids) == set(expected_map)
            for entry in entries:
                assert entry["quantityConsumed"] == expected_map[entry["productionBatchId"]]
                # Every entry carries the reference fields the forward trace
                # promises: batch code value, product model and generation time.
                assert entry["batchCode"]
                assert entry["productModelId"] == product_model_id
                assert entry["generatedAt"]

        # (B) The unconsumed supplier batch returns an empty list, not an error
        #     (Requirement 5.6).
        assert _forward_trace(client, unconsumed_batch["id"]) == []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_reverse_trace_for_nonexistent_batch_is_rejected() -> None:
    """反向追溯的生产批次不存在时拒绝并返回描述性错误。

    Reverse trace (反向追溯) is served through the batch detail endpoint's
    ``reverseTrace`` field. Requesting the detail of a production batch id that
    does not exist must be rejected with a descriptive error rather than
    returning any (empty) reverse-trace payload.

    _Requirements: 5.4_
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-suptrace-missing-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # No production batch has been generated, so id 999999 cannot exist.
        missing_batch_id = 999999
        response = client.get(f"/api/production-batches/{missing_batch_id}")

        # The query is rejected with a descriptive 404 error and no reverse
        # trace is returned (Requirement 5.4).
        assert response.status_code == 404, response.get_json()
        body = response.get_json()
        assert body["ok"] is False
        message = body["message"]
        assert isinstance(message, str) and message.strip(), (
            "a descriptive error message must be returned"
        )
        assert "data" not in body or body.get("data") is None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
