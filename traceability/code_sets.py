"""Per-unit product code sets (产品套码): the payload of one generated set.

A code set is what ``POST /api/products/<id>/code-sets`` generates for one unit
of a product: a finished-product main code plus one part code per BOM slot.
``product_code_set_dict`` reads a set back with its parts in slot order; the
generation, listing, batch-detail and download routes all return it.

Extracted from ``app.py`` when those routes moved to
``traceability/api/code_sets.py``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

__all__ = ["product_code_set_dict"]


def product_code_set_dict(database: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    part_rows = database.execute(
        """
        SELECT pcsp.position, pcsp.slot_name, pl.id AS part_label_id,
               pl.identification_code, pt.name AS part_name,
               s.name AS supplier_name
        FROM product_code_set_parts pcsp
        JOIN part_labels pl ON pl.id = pcsp.part_label_id
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        WHERE pcsp.product_code_set_id = ?
        ORDER BY pcsp.position
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "generationBatchId": row["generation_batch_id"] if "generation_batch_id" in row.keys() else None,
        "productModelId": row["product_model_id"],
        "productName": row["product_name"],
        "setCode": row["set_code"],
        "prefix": row["prefix"],
        "dateCode": row["date_code"],
        "sequence": row["daily_sequence"],
        "generatedAt": row["generated_at"],
        "machine": {
            "id": row["machine_id"],
            "sn": row["machine_sn"],
            "identificationCode": row["machine_code"],
            "qrUrl": f"/api/machines/{row['machine_id']}/qr",
        },
        "parts": [
            {
                "position": item["position"],
                "slotName": item["slot_name"],
                "partLabelId": item["part_label_id"],
                "partName": item["part_name"],
                "supplierName": item["supplier_name"],
                "identificationCode": item["identification_code"],
                "qrUrl": f"/api/part-labels/{item['part_label_id']}/qr",
            }
            for item in part_rows
        ],
        "downloadUrl": f"/api/product-code-sets/{row['id']}/qrcodes.zip",
    }
