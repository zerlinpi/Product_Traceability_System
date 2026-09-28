"""Scan-gun stock-in — HTTP layer.

Two things share this file because they are one workflow: looking a production QR
up, and confirming the stock-in for it. `/api/inbound-scan-records` is the
history of the second, and is here rather than in its own module for that reason.

Not to be confused with `/api/inbound-receipts` (供应收货): that records parts
arriving from a supplier and is pushed to Lingxing. This records *finished goods*
entering stock after a production QR is scanned. Both are "inbound" in English,
which is exactly why the two live in separate modules with separate docstrings.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph inside ``create_app``, so a guard moved into
another module disappears from the security document.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    require_admin_or_warehouse,
    require_product_model_access,
)
from traceability.codes import parse_batch_payload
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.idempotent_http import run_idempotent
from traceability.production import production_batch_registration
from traceability.production_orders import (
    production_order_data,
    production_order_progress_map,
    production_order_row,
)
from traceability.quality import require_quality_release, stock_in_block_reason
from traceability.responses import success
from traceability.validators import business_id, business_quantity

scan_gun_bp = Blueprint("scan_gun", __name__)


@scan_gun_bp.post("/api/scan-gun/lookup")
def scan_gun_lookup():
    require_admin_or_warehouse()
    payload = request.get_json(silent=True) or {}
    try:
        batch_code = parse_batch_payload(payload.get("code"))
    except ValueError as error:
        raise ApiError("生产二维码无效或对应生产订单不存在", 404) from error
    database = get_db()
    row = database.execute(
        """
        SELECT pro.*, po.po_no, po.quantity, po.part_type_id,
               pb.batch_code, pb.product_model_id, pb.planned_quantity,
               pb.prefix, pb.generated_at, pm.model_code, pm.name AS product_name
        FROM production_orders pro
        JOIN purchase_orders po ON po.id = pro.purchase_order_id
        JOIN production_batches pb ON pb.id = pro.production_batch_id
        JOIN product_models pm ON pm.id = pb.product_model_id
        WHERE pb.batch_code = ? COLLATE NOCASE
        """,
        (batch_code,),
    ).fetchone()
    if not row:
        raise ApiError("生产二维码无效或对应生产订单不存在", 404)
    require_product_model_access(row["product_model_id"])
    # Carry the flow state so the operator sees the batch's 登记/质量 status and
    # how much is already received *before* confirming the stock-in.
    progress = production_order_progress_map(database, [row]).get(row["id"], {})
    return success({**production_order_data(row), "progress": progress})


@scan_gun_bp.get("/api/inbound-scan-records")
def list_inbound_scan_records():
    # 成品扫码入库 history. Written by /api/scan-gun/inbound; the scan-gun page
    # lists it so the operator can confirm what was just received.
    require_admin_or_warehouse()
    database = get_db()
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError) as error:
        raise ApiError("查询条数无效") from error
    limit = min(max(limit, 1), 200)
    clauses: list[str] = []
    parameters: list[Any] = []
    order_value = request.args.get("productionOrderId")
    if order_value not in {None, ""}:
        clauses.append("isr.production_order_id = ?")
        parameters.append(business_id(order_value, "生产订单"))
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    rows = database.execute(
        f"""
        SELECT isr.*, pm.model_code, pm.name AS product_name,
               pb.batch_code, po.po_no,
               ps.on_hand
        FROM inbound_scan_records isr
        JOIN product_models pm ON pm.id = isr.product_model_id
        LEFT JOIN production_orders pro ON pro.id = isr.production_order_id
        LEFT JOIN production_batches pb ON pb.id = pro.production_batch_id
        LEFT JOIN purchase_orders po ON po.id = pro.purchase_order_id
        LEFT JOIN product_stock ps ON ps.product_model_id = isr.product_model_id
        {where_clause}
        ORDER BY isr.received_at DESC, isr.id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()
    return success(
        [
            {
                "id": row["id"],
                "productionOrderId": row["production_order_id"],
                "productionQrCode": row["batch_code"],
                "poNo": row["po_no"],
                "productModelId": row["product_model_id"],
                "productModelCode": row["model_code"],
                "productName": row["product_name"],
                "quantity": row["quantity"],
                "operatorName": row["operator_name"],
                "operatorUserId": row["operator_user_id"],
                "receivedAt": row["received_at"],
                "onHand": row["on_hand"],
            }
            for row in rows
        ]
    )


@scan_gun_bp.post("/api/scan-gun/inbound")
def scan_gun_inbound():
    return run_idempotent("scan-gun.inbound", _impl_scan_gun_inbound)


def _impl_scan_gun_inbound():
    require_admin_or_warehouse()
    payload = request.get_json(silent=True) or {}
    production_order_id = business_id(payload.get("productionOrderId"), "生产订单")
    quantity = business_quantity(payload.get("quantity"), "入库数量")
    database = get_db()
    order = production_order_row(database, production_order_id)
    if not order:
        raise ApiError("生产二维码无效或对应生产订单不存在", 404)
    require_product_model_access(order["product_model_id"])
    # 质量门禁: a 暂扣 batch never enters stock, and when 质量放行 is enabled the
    # batch must be registered and released first (Requirement: the quality
    # step has to actually gate the flow).
    registration = production_batch_registration(database, order["production_batch_id"])
    gate_reason = stock_in_block_reason(
        registration.get("qualityStatus") if registration.get("registered") else None,
        require_quality_release(database),
    )
    if gate_reason:
        raise ApiError(gate_reason, 409)
    timestamp = current_app.config["NOW_PROVIDER"]()
    database.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.execute(
            """
            INSERT INTO inbound_scan_records(
                production_order_id, product_model_id, quantity,
                operator_name, operator_user_id, received_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                production_order_id,
                order["product_model_id"],
                quantity,
                current_actor_name("系统管理员"),
                current_actor_id(),
                timestamp,
            ),
        )
        database.execute(
            """
            INSERT INTO product_stock(product_model_id, on_hand, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(product_model_id) DO UPDATE SET
                on_hand = product_stock.on_hand + excluded.on_hand,
                updated_at = excluded.updated_at
            """,
            (order["product_model_id"], quantity, timestamp),
        )
        latest = database.execute(
            "SELECT on_hand FROM product_stock WHERE product_model_id = ?",
            (order["product_model_id"],),
        ).fetchone()["on_hand"]
        record_audit_event(
            database,
            "FINISHED_GOODS_RECEIVED",
            "PRODUCTION_ORDER",
            str(production_order_id),
            related_object_code=order["batch_code"],
            payload={
                "inboundRecordId": cursor.lastrowid,
                "productModelId": order["product_model_id"],
                "quantity": quantity,
                "onHand": latest,
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    return success(
        {
            "id": cursor.lastrowid,
            "productionOrderId": production_order_id,
            "productModelId": order["product_model_id"],
            "quantity": quantity,
            "onHand": latest,
            "receivedAt": timestamp,
        },
        201,
    )

