"""Inbound receipts — HTTP layer.

Supplier goods receipts raised by the warehouse against a purchase order, and
their push to Lingxing. The push plumbing (service construction, endpoint
readiness, the in-progress guard) lives in ``traceability/lingxing_writes.py``
and is shared with purchase orders and inventory sync.

Guards are called in the route rather than inside a domain call: the permission
matrix is built by following the call graph inside ``create_app``, so a guard
that moved into another module would disappear from the security document.
"""

from __future__ import annotations

import json

from flask import Blueprint, current_app, request

from traceability.auth import (
    current_actor_id,
    current_actor_name,
    require_operations,
    require_warehouse,
)
from traceability.audit_events import record_audit_event
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.lingxing import LingxingError
from traceability.lingxing_writes import (
    PUSH_GUARD_TIMEOUT,
    ensure_lingxing_operation_ready,
    external_identifier,
    guard_is_stale,
    lingxing_service,
)
from traceability.pagination import parse_list_window
from traceability.purchasing import purchase_order_row
from traceability.receipts import inbound_receipt_data, inbound_receipt_row
from traceability.responses import success
from traceability.validators import business_id, business_quantity

inbound_receipts_bp = Blueprint("inbound_receipts", __name__)


@inbound_receipts_bp.post("/api/inbound-receipts")
def create_inbound_receipt():
    require_warehouse()
    payload = request.get_json(silent=True) or {}
    purchase_order_id = business_id(payload.get("purchaseOrderId"), "采购订单")
    quantity = business_quantity(payload.get("quantity"), "入库数量")
    database = get_db()
    po = purchase_order_row(database, purchase_order_id)
    if not po:
        raise ApiError("采购订单不存在", 404)
    timestamp = current_app.config["NOW_PROVIDER"]()
    database.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.execute(
            """
            INSERT INTO inbound_receipts(
                purchase_order_id, quantity, receiver, receiver_user_id,
                received_at, sync_status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (
                purchase_order_id,
                quantity,
                current_actor_name("系统管理员"),
                current_actor_id(),
                timestamp,
                timestamp,
            ),
        )
        record_audit_event(
            database,
            "INBOUND_RECEIVED",
            "INBOUND_RECEIPT",
            str(cursor.lastrowid),
            related_object_code=po["po_no"],
            payload={"purchaseOrderId": purchase_order_id, "quantity": quantity},
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    return success(inbound_receipt_data(inbound_receipt_row(database, cursor.lastrowid)), 201)

@inbound_receipts_bp.get("/api/inbound-receipts")
def list_inbound_receipts():
    # Operations needs read access in order to push receipts created by the
    # warehouse. Mutating receipt creation remains warehouse-only.
    #
    # Bounded: this table gains a row per receipt and the front end renders every
    # row it is given. COUNT(*) OVER () is evaluated before LIMIT, so the total
    # arrives with the window rather than costing a second scan.
    database = get_db()
    window = parse_list_window()
    rows = database.execute(
        """
        SELECT ir.*, po.po_no, po.sync_status AS purchase_order_sync_status,
               po.lingxing_po_id, s.name AS supplier_name,
               pt.part_code, pt.name AS part_name,
               pm.name AS product_model_name,
               COUNT(*) OVER () AS total_count
        FROM inbound_receipts ir
        JOIN purchase_orders po ON po.id = ir.purchase_order_id
        LEFT JOIN suppliers s ON s.id = po.supplier_id
        LEFT JOIN part_types pt ON pt.id = po.part_type_id
        LEFT JOIN product_models pm ON pm.id = po.product_model_id
        ORDER BY ir.received_at DESC, ir.id DESC
        LIMIT ? OFFSET ?
        """,
        (window.limit, window.offset),
    ).fetchall()
    total = rows[0]["total_count"] if rows else 0
    return window.apply(success([inbound_receipt_data(row) for row in rows]), total)

@inbound_receipts_bp.get("/api/inbound-receipts/<int:receipt_id>")
def get_inbound_receipt(receipt_id: int):
    row = inbound_receipt_row(get_db(), receipt_id)
    if not row:
        raise ApiError("入库收货记录不存在", 404)
    return success(inbound_receipt_data(row))

@inbound_receipts_bp.post("/api/inbound-receipts/<int:receipt_id>/push")
def push_inbound_receipt(receipt_id: int):
    require_operations()
    database = get_db()
    row = inbound_receipt_row(database, receipt_id)
    if not row:
        raise ApiError("入库收货记录不存在", 404)
    if row["purchase_order_sync_status"] != "PUSHED":
        raise ApiError("请先成功推送采购订单", 409)
    if row["sync_status"] == "PUSHED":
        return success({**inbound_receipt_data(row), "message": "该入库收货已推送"})
    service = lingxing_service(database)
    ensure_lingxing_operation_ready(service, "inbound_receipt")
    database.execute("BEGIN IMMEDIATE")
    try:
        current = database.execute(
            "SELECT sync_status, push_in_progress, push_started_at "
            "FROM inbound_receipts WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        if current["sync_status"] == "PUSHED":
            database.rollback()
            return success({**inbound_receipt_data(inbound_receipt_row(database, receipt_id)), "message": "该入库收货已推送"})
        guard_started_at = current_app.config["NOW_PROVIDER"]()
        if current["push_in_progress"] and not guard_is_stale(
            current["push_started_at"], guard_started_at, PUSH_GUARD_TIMEOUT
        ):
            raise ApiError("入库收货推送正在进行中", 409)
        # Stamping the start lets a later attempt recover an abandoned guard.
        database.execute(
            "UPDATE inbound_receipts SET push_in_progress = 1, push_started_at = ? "
            "WHERE id = ?",
            (guard_started_at, receipt_id),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise

    try:
        response = service.push(
            "inbound_receipt",
            {
                "localId": row["id"],
                "purchaseOrderId": row["lingxing_po_id"],
                "quantity": row["quantity"],
            },
        )
        lingxing_id = external_identifier(response, "inboundId", "receiptId", "id")
        if not lingxing_id:
            raise LingxingError("领星入库响应缺少对象标识")
        timestamp = current_app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            UPDATE inbound_receipts
            SET sync_status = 'PUSHED', push_in_progress = 0,
                lingxing_inbound_id = ?, lingxing_raw_response = ?,
                push_error = '', pushed_at = ?
            WHERE id = ?
            """,
            (lingxing_id, json.dumps(response, ensure_ascii=False), timestamp, receipt_id),
        )
        record_audit_event(
            database,
            "INBOUND_PUSHED",
            "INBOUND_RECEIPT",
            str(receipt_id),
            related_object_code=row["po_no"],
            payload={"result": "PUSHED", "lingxingInboundId": lingxing_id},
            occurred_at=timestamp,
        )
        database.commit()
    except Exception as error:
        database.rollback()
        message = error.message if isinstance(error, LingxingError) else str(error)
        timestamp = current_app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            UPDATE inbound_receipts
            SET sync_status = 'FAILED', push_in_progress = 0, push_error = ?
            WHERE id = ?
            """,
            (message, receipt_id),
        )
        record_audit_event(
            database,
            "INBOUND_PUSH_FAILED",
            "INBOUND_RECEIPT",
            str(receipt_id),
            related_object_code=row["po_no"],
            reason=message,
            payload={"result": "FAILED", "error": message},
            occurred_at=timestamp,
        )
        database.commit()
        if isinstance(error, (ApiError, LingxingError)):
            raise
        raise LingxingError(f"领星入库推送失败：{message}") from error
    return success({**inbound_receipt_data(inbound_receipt_row(database, receipt_id)), "message": "入库收货已推送"})

@inbound_receipts_bp.get("/api/inbound-receipts/<int:receipt_id>/sync-status")
def inbound_receipt_sync_status(receipt_id: int):
    require_operations()
    row = inbound_receipt_row(get_db(), receipt_id)
    if not row:
        raise ApiError("入库收货记录不存在", 404)
    return success(
        {
            "id": row["id"],
            "syncStatus": row["sync_status"],
            "lingxingId": row["lingxing_inbound_id"] or None,
            "pushedAt": row["pushed_at"],
            "pushError": row["push_error"],
        }
    )
