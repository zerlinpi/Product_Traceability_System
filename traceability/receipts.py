"""Supplier goods receipts (供应收货).

A receipt records parts arriving against a purchase order, and is then pushed to
Lingxing. The push semantics belong to ``lingxing_writes.py``; this module only
knows how to read a receipt back out of the database.

Extracted from ``app.py`` so the inbound-receipt blueprint can reach it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

__all__ = ["inbound_receipt_data", "inbound_receipt_row"]


def inbound_receipt_row(database: sqlite3.Connection, receipt_id: int):
    return database.execute(
        """
        SELECT ir.*, po.po_no, po.sync_status AS purchase_order_sync_status,
               po.lingxing_po_id, s.name AS supplier_name,
               pt.part_code, pt.name AS part_name,
               pm.name AS product_model_name
        FROM inbound_receipts ir
        JOIN purchase_orders po ON po.id = ir.purchase_order_id
        LEFT JOIN suppliers s ON s.id = po.supplier_id
        LEFT JOIN part_types pt ON pt.id = po.part_type_id
        LEFT JOIN product_models pm ON pm.id = po.product_model_id
        WHERE ir.id = ?
        """,
        (receipt_id,),
    ).fetchone()

def inbound_receipt_data(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    part_name = row["part_name"]
    if not part_name and "product_model_name" in keys:
        part_name = row["product_model_name"]
    return {
        "id": row["id"],
        "purchaseOrderId": row["purchase_order_id"],
        "poNo": row["po_no"],
        "supplierName": row["supplier_name"],
        "partCode": row["part_code"],
        "partName": part_name,
        "quantity": row["quantity"],
        "receiver": row["receiver"],
        "receiverUserId": row["receiver_user_id"],
        "receivedAt": row["received_at"],
        "syncStatus": row["sync_status"],
        "pushInProgress": bool(row["push_in_progress"]),
        "lingxingInboundId": row["lingxing_inbound_id"] or None,
        "pushError": row["push_error"],
        "createdAt": row["created_at"],
        "pushedAt": row["pushed_at"],
    }
