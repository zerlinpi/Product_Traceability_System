"""Production-batch registration and reverse trace.

Both are read/write helpers around the batch tables rather than HTTP concerns:
``production_batch_registration`` records a whole batch in one shot (the field
scans one QR and the entire batch is registered), and
``production_batch_reverse_trace`` answers "which supplier batches went into
this one?" — the question asked when a defect is traced back.

Extracted from ``app.py`` as part of the phase-3 split.
"""

from __future__ import annotations

import sqlite3
from typing import Any

__all__ = [
    "production_batch_registration",
    "production_batch_reverse_trace",
]


def production_batch_reverse_trace(
    database: sqlite3.Connection,
    production_batch_id: int,
) -> list[dict[str, Any]]:
    """Return the batch-level reverse-trace list for a production batch.

    Each entry names the consumed supplier inventory batch, its supplied part
    and the amount consumed (Requirements 5.1, 5.3, 10.2). When no supplier
    batch was consumed the list is empty rather than an error (Requirement 5.6).
    """
    rows = database.execute(
        """
        SELECT
            pbsc.supplier_inventory_batch_id AS supplier_inventory_batch_id,
            pbsc.part_type_id AS part_type_id,
            pbsc.quantity_consumed AS quantity_consumed,
            sib.batch_no AS batch_no,
            pt.part_code AS part_code,
            pt.name AS part_name
        FROM production_batch_supplier_consumption pbsc
        LEFT JOIN supplier_inventory_batches sib
            ON sib.id = pbsc.supplier_inventory_batch_id
        LEFT JOIN part_types pt ON pt.id = pbsc.part_type_id
        WHERE pbsc.production_batch_id = ?
        ORDER BY pbsc.id ASC
        """,
        (production_batch_id,),
    ).fetchall()
    return [
        {
            "supplierInventoryBatchId": row["supplier_inventory_batch_id"],
            "supplierBatchNo": row["batch_no"],
            "partTypeId": row["part_type_id"],
            "partCode": row["part_code"],
            "partName": row["part_name"],
            "quantityConsumed": row["quantity_consumed"],
        }
        for row in rows
    ]


def production_batch_registration(
    database: sqlite3.Connection,
    production_batch_id: int,
) -> dict[str, Any]:
    """Summarize whether a production batch has a batch registration record.

    A batch has at most one ``batch_trace_records`` row (Requirement 2). This
    surfaces the registration status for the detail view (design 6.2).
    """
    row = database.execute(
        """
        SELECT id, registered_quantity, quality_status, status_reason,
               operator_name, completed_by_user_id, registered_at,
               status_updated_at
        FROM batch_trace_records
        WHERE production_batch_id = ?
        """,
        (production_batch_id,),
    ).fetchone()
    if not row:
        return {"registered": False}
    return {
        "registered": True,
        "id": row["id"],
        "registeredQuantity": row["registered_quantity"],
        "qualityStatus": row["quality_status"],
        "statusReason": row["status_reason"],
        "operatorName": row["operator_name"],
        "completedByUserId": row["completed_by_user_id"],
        "registeredAt": row["registered_at"],
        "statusUpdatedAt": row["status_updated_at"],
    }
