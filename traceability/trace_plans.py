"""Trace plans (BOM): the ordered part slots a product is assembled from.

A trace plan lists, position by position, which part type each slot takes and
optionally which supplier inventory batch it draws from. Product management
creates and replaces plans, the trace-plan routes list and activate them, and
the legacy station scan and record correction check scanned parts against the
slots. The read side therefore lives here, where all of them can import it.

``_TRACE_PLAN_SLOT_SELECT`` is shared on purpose: the single-plan lookup below
and the batched products list in ``products.py`` must build identical slot rows,
which is why that module imports it despite the underscore.

Extracted from ``app.py`` when those routes moved to ``traceability/api/``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from traceability.serializers import _trace_plan_slot_dict

__all__ = [
    "MAX_REQUIRED_PARTS",
    "_TRACE_PLAN_SLOT_SELECT",
    "get_plan_summary",
    "trace_plan_dict",
    "trace_plan_slots",
]


# Caps the expanded number of component slots per product (sum of per-component
# quantities) accepted by ``POST /api/products``. Product edits and
# ``POST /api/trace-plans`` enforce the same cap, which is why it lives with the
# BOM rather than with either route.
MAX_REQUIRED_PARTS = 20

# Shared SELECT for a trace plan's slots joined to part / supplier / inventory
# data. Reused by the single-plan lookup and the batched products-list path so
# both produce identical slot rows.
_TRACE_PLAN_SLOT_SELECT = """
        SELECT tps.*, pt.part_code, pt.name AS part_name, pt.specification,
               pt.minimum_stock,
               pt.category_code, pt.category_name,
               s.id AS supplier_id, s.supplier_code, s.name AS supplier_name,
               sib.batch_no AS inventory_batch_no,
               sib.quantity_received AS inventory_quantity_received,
               sib.quantity_available AS inventory_quantity_available,
               sib.production_date AS inventory_production_date,
               sib.received_date AS inventory_received_date,
               sib.active AS inventory_batch_active
        FROM trace_plan_slots tps
        JOIN part_types pt ON pt.id = tps.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN supplier_inventory_batches sib ON sib.id = tps.supplier_inventory_batch_id
"""


def trace_plan_slots(database: sqlite3.Connection, plan_id: int | None) -> list[dict[str, Any]]:
    if not plan_id:
        return []
    rows = database.execute(
        _TRACE_PLAN_SLOT_SELECT + " WHERE tps.trace_plan_id = ? ORDER BY tps.position",
        (plan_id,),
    ).fetchall()
    return [_trace_plan_slot_dict(row) for row in rows]


def trace_plan_dict(database: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    slots = trace_plan_slots(database, row["id"])
    keys = set(row.keys())
    return {
        "id": row["id"],
        "modelCode": row["model_code"],
        "productModelId": row["product_model_id"] if "product_model_id" in keys else None,
        "productModelName": row["product_model_name"] if "product_model_name" in keys else None,
        "productFamilyId": row["product_family_id"] if "product_family_id" in keys else None,
        "productFamilyCode": row["product_family_code"] if "product_family_code" in keys else None,
        "productFamilyName": row["product_family_name"] if "product_family_name" in keys else None,
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "activatedAt": row["activated_at"],
        "usedMachineCount": row["used_machine_count"] if "used_machine_count" in keys else 0,
        "slots": slots,
    }


def get_plan_summary(database: sqlite3.Connection, plan_id: int | None) -> dict[str, Any] | None:
    if not plan_id:
        return None
    row = database.execute(
        """
        SELECT tp.*, pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name
        FROM trace_plans tp
        LEFT JOIN product_models pm ON pm.id = tp.product_model_id
        LEFT JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE tp.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if not row:
        return None
    return trace_plan_dict(database, row)
