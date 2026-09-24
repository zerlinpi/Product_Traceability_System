"""Supplier-inventory deduction.

The write path that consumes stock when a production batch is generated. It is
the one place in the system that must not oversell: two concurrent generations
contending for the same supplier batch have to serialise, which is why the
caller opens a ``BEGIN IMMEDIATE`` transaction and this module re-checks the
balance inside it rather than trusting a read taken earlier.

Extracted from ``app.py`` as part of the phase-3 split, so the production-batch
blueprint can reach it.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from traceability.errors import ApiError

__all__ = [
    "compute_batch_inventory_requirements",
    "deduct_batch_inventory",
]


def compute_batch_inventory_requirements(
    database: sqlite3.Connection,
    trace_plan_id: int,
    quantity: int,
) -> list[dict[str, int]]:
    """Compute per-supplier-batch deduction requirements for a production batch.

    Per-unit usage for a supplier batch equals the number of ``trace_plan_slots``
    in the given active trace plan that bind that supplier inventory batch (see
    design "每套用量（Per_Unit_Usage）的落地口径"). The required deduction for a
    batch of ``quantity`` units is ``quantity × per-unit usage``. Only supplier
    batches whose per-unit usage is greater than 0 (i.e. bound by at least one
    slot) contribute a requirement; slots without a bound supplier batch consume
    nothing.

    Returns a list of ``{"supplier_inventory_batch_id", "part_type_id",
    "per_unit_usage", "required"}`` dicts ordered by supplier batch id. The
    ``part_type_id`` reflects the part the slot expects; requirements are
    aggregated per supplier inventory batch (Requirement 4.1).
    """
    slots = database.execute(
        """
        SELECT supplier_inventory_batch_id, part_type_id
        FROM trace_plan_slots
        WHERE trace_plan_id = ? AND supplier_inventory_batch_id IS NOT NULL
        ORDER BY position
        """,
        (trace_plan_id,),
    ).fetchall()

    per_unit_usage: dict[int, int] = defaultdict(int)
    part_type_by_batch: dict[int, int] = {}
    for slot in slots:
        inventory_batch_id = slot["supplier_inventory_batch_id"]
        per_unit_usage[inventory_batch_id] += 1
        part_type_by_batch.setdefault(inventory_batch_id, slot["part_type_id"])

    requirements: list[dict[str, int]] = []
    for inventory_batch_id in sorted(per_unit_usage):
        usage = per_unit_usage[inventory_batch_id]
        if usage <= 0:
            continue
        requirements.append(
            {
                "supplier_inventory_batch_id": inventory_batch_id,
                "part_type_id": part_type_by_batch[inventory_batch_id],
                "per_unit_usage": usage,
                "required": quantity * usage,
            }
        )
    return requirements


def deduct_batch_inventory(
    database: sqlite3.Connection,
    *,
    trace_plan_id: int,
    product_model_id: int,
    quantity: int,
    production_batch_id: int,
    batch_code: str,
    actor_user_id: int | None,
    timestamp: str,
) -> list[dict[str, int]]:
    """Deduct supplier inventory for a production batch within an open transaction.

    This helper MUST be called inside an already-open ``BEGIN IMMEDIATE``
    transaction (opened by the caller, e.g. the production-batch generation
    endpoint) so that concurrent batch generations serialize on the shared
    supplier batch balances and their cumulative deductions never exceed the
    available stock (Requirement 4.5).

    For each supplier inventory batch bound by the active trace plan with a
    per-unit usage greater than 0, the required deduction is ``quantity ×
    per-unit usage`` (Requirement 4.1). A sufficiency check is performed across
    all required parts first; if any part's required deduction exceeds its
    supplier batch's current available balance, an ``ApiError`` naming the short
    part is raised and no balance is modified and no ``ISSUE`` movement is
    written (Requirement 4.2 — the caller's transaction rolls back, leaving no
    partial deduction). Otherwise each batch balance is reduced and exactly one
    ``ISSUE`` row is written to ``supplier_inventory_movements`` carrying the
    ``production_batch_id`` (Requirements 4.1, 4.3).

    Returns the consumption records (``supplier_inventory_batch_id``,
    ``part_type_id``, ``quantity_consumed``) for the caller to persist as
    ``production_batch_supplier_consumption`` rows.
    """
    requirements = compute_batch_inventory_requirements(
        database, trace_plan_id, quantity
    )

    # Sufficiency check first: any shortfall fails the whole operation before
    # touching any balance or writing any movement (Requirement 4.2).
    inventories: dict[int, sqlite3.Row] = {}
    for requirement in requirements:
        inventory_batch_id = requirement["supplier_inventory_batch_id"]
        inventory = database.execute(
            """
            SELECT sib.*, pt.name AS part_name
            FROM supplier_inventory_batches sib
            JOIN part_types pt ON pt.id = sib.part_type_id
            WHERE sib.id = ?
            """,
            (inventory_batch_id,),
        ).fetchone()
        if not inventory or not inventory["active"]:
            raise ApiError("产品绑定的供应商批次已停用，请先编辑产品", 409)
        if inventory["quantity_available"] < requirement["required"]:
            raise ApiError(
                f"{inventory['part_name']} / {inventory['batch_no']} 库存不足："
                f"需要 {requirement['required']}，可用 {inventory['quantity_available']}",
                409,
            )
        inventories[inventory_batch_id] = inventory

    # All parts have sufficient stock: deduct balances and write ISSUE movements.
    consumption: list[dict[str, int]] = []
    for requirement in requirements:
        inventory_batch_id = requirement["supplier_inventory_batch_id"]
        required_quantity = requirement["required"]
        inventory = inventories[inventory_batch_id]
        balance_after = inventory["quantity_available"] - required_quantity
        database.execute(
            """
            UPDATE supplier_inventory_batches
            SET quantity_available = ?, updated_at = ? WHERE id = ?
            """,
            (balance_after, timestamp, inventory_batch_id),
        )
        database.execute(
            """
            INSERT INTO supplier_inventory_movements(
                inventory_batch_id, movement_type, quantity_change,
                balance_after, product_model_id, production_batch_id,
                actor_user_id, reason, occurred_at
            ) VALUES (?, 'ISSUE', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inventory_batch_id, -required_quantity, balance_after,
                product_model_id, production_batch_id, actor_user_id,
                f"生成 {batch_code} 生产批次",
                timestamp,
            ),
        )
        consumption.append(
            {
                "supplier_inventory_batch_id": inventory_batch_id,
                "part_type_id": requirement["part_type_id"],
                "quantity_consumed": required_quantity,
            }
        )
    return consumption
