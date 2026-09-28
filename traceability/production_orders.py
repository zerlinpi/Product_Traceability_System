"""Production orders — a factory order raised from a purchase order.

Not to be confused with ``traceability/production.py``, which holds *batch*
traceability (registering a whole batch from one QR, and reverse-tracing a batch
back to the supplier lots it consumed). A production order is the earlier step:
"make N units for this purchase order", which the batch is later generated
against.

This module depends on ``traceability/purchasing.py`` for the purchase-order
lookups it is raised from. That direction is real — a production order cannot
exist without a purchase order — and nothing in purchasing imports back.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from traceability.auth import (
    current_actor_id,
    current_actor_name,
)
from traceability.audit_events import record_audit_event
from traceability.codes import (
    batch_identification_code,
    new_batch_code,
    normalize_entity_code,
)
from traceability.errors import ApiError
from traceability.purchasing import (
    purchase_order_planned_quantity,
)
from traceability.quality import require_quality_release, stock_in_block_reason

__all__ = [
    "generate_production_order_for_po",
    "production_order_data",
    "production_order_progress_map",
    "production_order_row",
]


def production_order_row(database: sqlite3.Connection, production_order_id: int):
    return database.execute(
        """
        SELECT pro.*, po.po_no, po.quantity, po.part_type_id,
               pb.batch_code, pb.product_model_id, pb.planned_quantity,
               pb.prefix, pb.generated_at,
               pm.model_code, pm.name AS product_name
        FROM production_orders pro
        JOIN purchase_orders po ON po.id = pro.purchase_order_id
        JOIN production_batches pb ON pb.id = pro.production_batch_id
        JOIN product_models pm ON pm.id = pb.product_model_id
        WHERE pro.id = ?
        """,
        (production_order_id,),
    ).fetchone()

def production_order_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "purchaseOrderId": row["purchase_order_id"],
        "poNo": row["po_no"],
        "productionBatchId": row["production_batch_id"],
        "productionQrCode": row["batch_code"],
        "identificationCode": batch_identification_code(row["batch_code"]),
        "productModelId": row["product_model_id"],
        "productModelCode": row["model_code"],
        "productName": row["product_name"],
        "quantity": row["planned_quantity"],
        "isExternal": bool(row["is_external"]) if "is_external" in row.keys() else False,
        "createdBy": row["created_by"],
        "createdByUserId": row["created_by_user_id"],
        "createdAt": row["created_at"],
        "downloadUrl": f"/api/production-orders/{row['id']}/qr",
        "products": [
            {
                "productModelId": row["product_model_id"],
                "modelCode": row["model_code"],
                "name": row["product_name"],
                "quantity": row["planned_quantity"],
            }
        ],
    }

def production_order_progress_map(
    database: sqlite3.Connection, orders: list[sqlite3.Row]
) -> dict[int, dict[str, Any]]:
    """Flow state per production order: 登记 → 质量 → 入库.

    Resolves the batch registration/quality status and the cumulative
    received quantity for every order in a fixed number of queries, so the
    warehouse can see which step each order is on (and why a stock-in is
    refused) instead of discovering it only on scan.
    """
    if not orders:
        return {}
    batch_ids = [row["production_batch_id"] for row in orders]
    order_ids = [row["id"] for row in orders]
    registrations = {
        r["production_batch_id"]: r
        for r in database.execute(
            f"""
            SELECT production_batch_id, registered_quantity, quality_status
            FROM batch_trace_records
            WHERE production_batch_id IN ({",".join("?" for _ in batch_ids)})
            """,
            batch_ids,
        ).fetchall()
    }
    received = {
        r["production_order_id"]: r["total"] or 0
        for r in database.execute(
            f"""
            SELECT production_order_id, SUM(quantity) AS total
            FROM inbound_scan_records
            WHERE production_order_id IN ({",".join("?" for _ in order_ids)})
            GROUP BY production_order_id
            """,
            order_ids,
        ).fetchall()
    }
    gate = require_quality_release(database)
    progress: dict[int, dict[str, Any]] = {}
    for row in orders:
        registration = registrations.get(row["production_batch_id"])
        quality_status = registration["quality_status"] if registration else None
        planned = row["planned_quantity"] or 0
        received_quantity = received.get(row["id"], 0)
        blocked = stock_in_block_reason(quality_status, gate)
        progress[row["id"]] = {
            "registered": registration is not None,
            "registeredQuantity": (
                registration["registered_quantity"] if registration else None
            ),
            "qualityStatus": quality_status,
            "receivedQuantity": received_quantity,
            "remainingQuantity": max(planned - received_quantity, 0),
            "fullyReceived": planned > 0 and received_quantity >= planned,
            "qualityReleaseRequired": gate,
            "canStockIn": blocked is None,
            "stockInBlockedReason": blocked,
        }
    return progress

def generate_production_order_for_po(
    database: sqlite3.Connection,
    po: sqlite3.Row,
    product: sqlite3.Row,
    *,
    external: bool,
    timestamp: str,
) -> int:
    """Create a production batch + order (minting the batch QR) for one PO.

    Resolves the linked product (BOM optional), mints a unique batch code and
    inserts the batch and the production order in a single ``BEGIN IMMEDIATE``
    transaction. ``external`` marks the order as 外采 (externally procured) —
    the trace code is still minted so downstream inbound / stock stays
    uniform. Returns the new production order id. The caller is responsible
    for the "already generated" pre-check; a concurrent duplicate still
    raises a 409 via the ``purchase_order_id`` unique constraint.

    ``product`` is resolved and authorized by the caller. Resolving it here
    would put ``require_product_model_access()`` out of reach of
    ``tools/extract_routes.py``, which builds the permission matrix by
    following the call graph inside ``create_app`` and cannot see into this
    module — the two POST routes would then be documented as not carrying a
    product-scope guard at all.
    """
    planned_quantity = purchase_order_planned_quantity(po)
    try:
        date_code = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        date_code = datetime.now().astimezone().strftime("%Y%m%d")
    prefix = normalize_entity_code(product["serial_prefix"] or product["model_code"], "生产二维码前缀")
    batch_code = new_batch_code(date_code, prefix)
    database.execute("BEGIN IMMEDIATE")
    try:
        batch_cursor = database.execute(
            """
            INSERT INTO production_batches(
                batch_code, product_model_id, trace_plan_id, prefix,
                planned_quantity, generated_by, generated_by_user_id, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_code,
                product["id"],
                product["trace_plan_id"],
                prefix,
                planned_quantity,
                current_actor_name("系统管理员"),
                current_actor_id(),
                timestamp,
            ),
        )
        order_cursor = database.execute(
            """
            INSERT INTO production_orders(
                purchase_order_id, production_batch_id, is_external,
                created_by, created_by_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                po["id"],
                batch_cursor.lastrowid,
                1 if external else 0,
                current_actor_name("系统管理员"),
                current_actor_id(),
                timestamp,
            ),
        )
        record_audit_event(
            database,
            "PRODUCTION_ORDER_CREATED",
            "PRODUCTION_ORDER",
            str(order_cursor.lastrowid),
            related_object_code=po["po_no"],
            payload={
                "purchaseOrderId": po["id"],
                "productionBatchId": batch_cursor.lastrowid,
                "batchCode": batch_code,
                "productModelId": product["id"],
                "quantity": planned_quantity,
                "isExternal": bool(external),
            },
            occurred_at=timestamp,
        )
        database.commit()
    except sqlite3.IntegrityError as error:
        database.rollback()
        if "production_orders.purchase_order_id" in str(error):
            raise ApiError("该采购订单已生成生产订单", 409) from error
        raise
    except Exception:
        database.rollback()
        raise
    return order_cursor.lastrowid
