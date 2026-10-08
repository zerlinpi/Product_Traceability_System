"""Purchase orders — HTTP layer.

Thin by design: parse the request, delegate to ``traceability.purchasing``, shape
the response. The rules themselves (what an order is, who may see it, what a push
means) live in the domain module, and the Lingxing client and Excel builder stay
where they were.

Two things here are not incidental:

* ``require_operations()`` is called in the route, not inside the domain call.
  ``tools/extract_routes.py`` builds the permission matrix by following the call
  graph inside ``create_app``, so a guard that moved into the domain module would
  become invisible to it and the route would be documented as merely "any
  authenticated". Authorization is the HTTP layer's job anyway.
* ``PURCHASE_ORDER_EXPORT_COLUMNS`` is imported from the domain module rather than
  redefined. The 48-column template is a business contract with Lingxing; there is
  exactly one definition of it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from io import BytesIO

from flask import Blueprint, current_app, request, send_file

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_user,
    require_operations,
)
from traceability.codes import new_event_id
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.idempotent_http import run_idempotent
from traceability.pagination import parse_list_window
from traceability.purchasing import (
    PURCHASE_ORDER_EXPORT_COLUMNS,
    parse_purchase_order_input,
    purchase_order_data,
    purchase_order_export_values,
    purchase_order_row,
    push_purchase_order_record,
    query_purchase_orders,
)
from traceability.responses import success
from traceability.validators import safe_archive_name
from traceability.xlsx_export import build_traceability_xlsx

purchase_orders_bp = Blueprint("purchase_orders", __name__)


@purchase_orders_bp.post("/api/purchase-orders")
def create_purchase_order():
    return run_idempotent("purchase-orders.create", _impl_create_purchase_order)


def _impl_create_purchase_order():
    # Operations fill the purchase-order template directly. Supplier / 商品
    # links are optional; the product link is kept ("和产品挂钩") and every
    # order records its creator (采购人).
    require_operations()
    payload = request.get_json(silent=True) or {}
    database = get_db()
    actor = current_user()
    actor_role = actor["role"] if actor else ""

    product_model_id, supplier_id, part_type_id, quantity, fields = (
        parse_purchase_order_input(database, payload, actor_role)
    )

    if product_model_id is None and part_type_id is None and not fields:
        raise ApiError("请选择产品并填写采购单信息")

    timestamp = current_app.config["NOW_PROVIDER"]()
    po_no = str(fields.get("采购单号") or "").strip() or (
        f"PO-{re.sub(r'[^0-9]', '', timestamp)[:14]}-{new_event_id()[-8:]}"
    )
    fields["采购单号"] = po_no
    database.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.execute(
            """
            INSERT INTO purchase_orders(
                po_no, supplier_id, part_type_id, product_model_id, quantity,
                fields_json, sync_status, created_by, created_by_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """,
            (
                po_no,
                supplier_id,
                part_type_id,
                product_model_id,
                quantity,
                json.dumps(fields, ensure_ascii=False),
                current_actor_name("系统管理员"),
                current_actor_id(),
                timestamp,
            ),
        )
        record_audit_event(
            database,
            "PO_CREATED",
            "PURCHASE_ORDER",
            po_no,
            payload={
                "productModelId": product_model_id,
                "supplierId": supplier_id,
                "partTypeId": part_type_id,
                "quantity": quantity,
                "syncStatus": "PENDING",
            },
            occurred_at=timestamp,
        )
        database.commit()
    except sqlite3.IntegrityError as error:
        database.rollback()
        if "po_no" in str(error).lower():
            raise ApiError("采购单号重复，请重试", 409) from error
        raise
    except Exception:
        database.rollback()
        raise
    return success(purchase_order_data(purchase_order_row(database, cursor.lastrowid)), 201)


@purchase_orders_bp.get("/api/purchase-orders")
def list_purchase_orders():
    # Bounded: this table gains a row per order and the front end renders every
    # row it is handed. The total travels in a header, so a caller can tell a
    # window from the whole table. The export path calls the same query without
    # a window, because a file is not a screen.
    window = parse_list_window()
    rows = query_purchase_orders(
        sync_status=request.args.get("syncStatus"),
        supplier_id=request.args.get("supplierId"),
        created_from=request.args.get("from"),
        created_to=request.args.get("to"),
        limit=window.limit,
        offset=window.offset,
    )
    total = rows[0]["total_count"] if rows else 0
    return window.apply(success([purchase_order_data(row) for row in rows]), total)


@purchase_orders_bp.get("/api/purchase-orders/<int:purchase_order_id>")
def get_purchase_order(purchase_order_id: int):
    row = purchase_order_row(get_db(), purchase_order_id)
    if not row:
        raise ApiError("采购订单不存在", 404)
    return success(purchase_order_data(row))


@purchase_orders_bp.put("/api/purchase-orders/<int:purchase_order_id>")
def update_purchase_order(purchase_order_id: int):
    # Operations may revise their own orders; admins may revise any. A
    # PUSHED order is locked so the local record stays consistent with what
    # was sent to Lingxing.
    require_operations()
    payload = request.get_json(silent=True) or {}
    database = get_db()
    actor = current_user()
    actor_role = actor["role"] if actor else ""
    existing = purchase_order_row(database, purchase_order_id)
    if not existing:
        raise ApiError("采购订单不存在", 404)
    if actor_role == "OPERATIONS" and existing["created_by_user_id"] != current_actor_id():
        raise ApiError("只能修改自己创建的采购订单", 403)
    if existing["sync_status"] == "PUSHED":
        raise ApiError("已推送的采购订单不可修改", 409)

    product_model_id, supplier_id, part_type_id, quantity, fields = (
        parse_purchase_order_input(database, payload, actor_role)
    )
    if product_model_id is None and part_type_id is None and not fields:
        raise ApiError("请选择产品并填写采购单信息")
    # The document number is immutable; keep the original po_no.
    fields["采购单号"] = existing["po_no"]
    # A revised order that previously failed to push returns to PENDING so it
    # can be retried cleanly.
    next_status = "PENDING" if existing["sync_status"] == "FAILED" else existing["sync_status"]
    timestamp = current_app.config["NOW_PROVIDER"]()
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            """
            UPDATE purchase_orders
            SET supplier_id = ?, part_type_id = ?, product_model_id = ?,
                quantity = ?, fields_json = ?, sync_status = ?,
                push_error = CASE WHEN ? = 'PENDING' THEN '' ELSE push_error END
            WHERE id = ?
            """,
            (
                supplier_id,
                part_type_id,
                product_model_id,
                quantity,
                json.dumps(fields, ensure_ascii=False),
                next_status,
                next_status,
                purchase_order_id,
            ),
        )
        record_audit_event(
            database,
            "PO_UPDATED",
            "PURCHASE_ORDER",
            existing["po_no"],
            payload={
                "productModelId": product_model_id,
                "supplierId": supplier_id,
                "partTypeId": part_type_id,
                "quantity": quantity,
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    return success(purchase_order_data(purchase_order_row(database, purchase_order_id)))


@purchase_orders_bp.delete("/api/purchase-orders/<int:purchase_order_id>")
def delete_purchase_order(purchase_order_id: int):
    require_operations()
    database = get_db()
    actor = current_user()
    actor_role = actor["role"] if actor else ""
    existing = purchase_order_row(database, purchase_order_id)
    if not existing:
        raise ApiError("采购订单不存在", 404)
    if actor_role == "OPERATIONS" and existing["created_by_user_id"] != current_actor_id():
        raise ApiError("只能删除自己创建的采购订单", 403)
    referenced = database.execute(
        """
        SELECT (SELECT COUNT(*) FROM inbound_receipts WHERE purchase_order_id = ?)
             + (SELECT COUNT(*) FROM production_orders WHERE purchase_order_id = ?) AS n
        """,
        (purchase_order_id, purchase_order_id),
    ).fetchone()["n"]
    if referenced:
        raise ApiError("该采购订单已被收货或生产订单引用，无法删除", 409)
    database.execute("BEGIN IMMEDIATE")
    try:
        record_audit_event(
            database,
            "PO_DELETED",
            "PURCHASE_ORDER",
            existing["po_no"],
            payload={"syncStatus": existing["sync_status"]},
        )
        database.execute(
            "DELETE FROM purchase_orders WHERE id = ?", (purchase_order_id,)
        )
        database.commit()
    except sqlite3.IntegrityError as error:
        database.rollback()
        raise ApiError("该采购订单已被其他数据引用，无法删除", 409) from error
    except Exception:
        database.rollback()
        raise
    return success({"id": purchase_order_id, "poNo": existing["po_no"]})


@purchase_orders_bp.post("/api/purchase-orders/<int:purchase_order_id>/push")
def push_purchase_order(purchase_order_id: int):
    # Authorization sits in the route, not in the domain call: the permission
    # matrix is built by following the call graph inside create_app, so a
    # guard buried in traceability/purchasing.py would be invisible to it and
    # the route would be documented as merely "any authenticated".
    require_operations()
    # ``orderSn`` lets operations name the Lingxing 采购单号 explicitly when it
    # differs from our document number; omitted, it is resolved from the
    # order itself.
    payload = request.get_json(silent=True) or {}
    return success(
        push_purchase_order_record(purchase_order_id, payload.get("orderSn"))
    )


@purchase_orders_bp.get("/api/purchase-orders/<int:purchase_order_id>/sync-status")
def purchase_order_sync_status(purchase_order_id: int):
    require_operations()
    row = purchase_order_row(get_db(), purchase_order_id)
    if not row:
        raise ApiError("采购订单不存在", 404)
    return success(
        {
            "id": row["id"],
            "syncStatus": row["sync_status"],
            "lingxingId": row["lingxing_po_id"] or None,
            "pushedAt": row["pushed_at"],
            "pushError": row["push_error"],
        }
    )


@purchase_orders_bp.get("/api/purchase-orders/export")
def export_purchase_orders():
    # Batch export: one workbook with every visible order as a row (operations
    # see only their own). Uses the same filters as the list and is lenient so
    # a single legacy order can never fail the whole file.
    require_operations()
    rows = query_purchase_orders(
        sync_status=request.args.get("syncStatus"),
        supplier_id=request.args.get("supplierId"),
        created_from=request.args.get("from"),
        created_to=request.args.get("to"),
    )
    # Build each order's column map once, not once per cell. The previous form
    # indexed ``purchase_order_export_values(row)[0]`` inside the inner loop, so
    # a 48-column template rebuilt the same dictionary 48 times per order —
    # 96,000 times for 2,000 orders, which cost both the time and the garbage.
    table = []
    for row in rows:
        values, _is_legacy_linked = purchase_order_export_values(row)
        table.append([values[column] for column in PURCHASE_ORDER_EXPORT_COLUMNS])
    workbook = build_traceability_xlsx(PURCHASE_ORDER_EXPORT_COLUMNS, table)
    filename = f"采购订单_批量_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        BytesIO(workbook),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@purchase_orders_bp.get("/api/purchase-orders/<int:purchase_order_id>/export")
def export_purchase_order(purchase_order_id: int):
    require_operations()
    row = purchase_order_row(get_db(), purchase_order_id)
    if not row:
        raise ApiError("采购订单不存在", 404)
    values, is_legacy_linked = purchase_order_export_values(row)

    # Legacy supplier/part-linked orders still enforce their required columns
    # so the historical export contract (and its rejection path) is preserved.
    if is_legacy_linked:
        required = [
            "标识号", "供应商", "含税", "费用分配方式", "采购币种",
            "采购仓库", "SKU", "实际采购量", "含税单价",
        ]
        missing = [name for name in required if values.get(name) in {None, ""}]
        if missing:
            raise ApiError(f"采购单缺少必填列：{', '.join(missing)}")

    workbook = build_traceability_xlsx(
        PURCHASE_ORDER_EXPORT_COLUMNS,
        [[values[column] for column in PURCHASE_ORDER_EXPORT_COLUMNS]],
    )
    return send_file(
        BytesIO(workbook),
        as_attachment=True,
        download_name=f"采购订单_{safe_archive_name(row['po_no'])}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@purchase_orders_bp.get("/api/purchase-orders/<int:purchase_order_id>/factory-progress")
def purchase_order_factory_progress(purchase_order_id: int):
    require_operations()
    database = get_db()
    po = purchase_order_row(database, purchase_order_id)
    if not po:
        raise ApiError("采购订单不存在", 404)
    row = database.execute(
        """
        SELECT pro.id AS production_order_id,
               COALESCE(SUM(isr.quantity), 0) AS latest_quantity
        FROM production_orders pro
        LEFT JOIN inbound_scan_records isr ON isr.production_order_id = pro.id
        WHERE pro.purchase_order_id = ?
        GROUP BY pro.id
        """,
        (purchase_order_id,),
    ).fetchone()
    return success(
        {
            "purchaseOrderId": purchase_order_id,
            "productionOrderGenerated": bool(row),
            "productionOrderId": row["production_order_id"] if row else None,
            "latestQuantity": int(row["latest_quantity"]) if row else 0,
        }
    )

