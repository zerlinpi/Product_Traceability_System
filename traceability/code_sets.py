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
from collections.abc import Iterable
from typing import Any

__all__ = ["code_set_dicts", "parts_by_code_set", "product_code_set_dict"]


_PARTS_SQL = """
        SELECT pcsp.product_code_set_id AS set_id,
               pcsp.position, pcsp.slot_name, pl.id AS part_label_id,
               pl.identification_code, pt.name AS part_name,
               s.name AS supplier_name
        FROM product_code_set_parts pcsp
        JOIN part_labels pl ON pl.id = pcsp.part_label_id
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        WHERE pcsp.product_code_set_id IN ({placeholders})
        ORDER BY pcsp.product_code_set_id, pcsp.position
"""


def parts_by_code_set(
    database: sqlite3.Connection, set_ids: Iterable[int]
) -> dict[int, list[sqlite3.Row]]:
    """Fetch the parts of many code sets in one query.

    Reading a set back one at a time costs a query each, which is invisible on a
    single set and ruinous on an export: a ZIP of 10,000 codes ran 10,001
    queries. Callers that already have the rows in hand should use this and pass
    the result to ``product_code_set_dict``.
    """
    ids = list(dict.fromkeys(set_ids))
    if not ids:
        return {}
    grouped: dict[int, list[sqlite3.Row]] = {set_id: [] for set_id in ids}
    # SQLite's default limit on host parameters is 999, so ask in batches.
    for start in range(0, len(ids), 900):
        chunk = ids[start : start + 900]
        placeholders = ", ".join("?" for _ in chunk)
        for row in database.execute(
            _PARTS_SQL.format(placeholders=placeholders), chunk
        ):
            grouped[row["set_id"]].append(row)
    return grouped


def code_set_dicts(
    database: sqlite3.Connection, rows: Iterable[sqlite3.Row]
) -> list[dict[str, Any]]:
    """Several code sets with their parts, reading the parts in batches.

    The obvious ``[product_code_set_dict(db, row) for row in rows]`` is a query
    per row. Every list and export route should go through this instead.
    """
    rows = list(rows)
    parts = parts_by_code_set(database, [row["id"] for row in rows])
    return [
        product_code_set_dict(database, row, parts=parts.get(row["id"], []))
        for row in rows
    ]


def product_code_set_dict(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    parts: list[sqlite3.Row] | None = None,
) -> dict[str, Any]:
    """One code set with its parts in slot order.

    ``parts`` lets a caller that already fetched them for a whole page or export
    skip the per-row query; without it the row's own parts are read here.
    """
    if parts is None:
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
    else:
        part_rows = parts
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
