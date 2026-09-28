"""Production orders — HTTP layer.

Thin by design: parse the request, delegate to
``traceability.production_orders``, shape the response.

As with purchase orders, ``require_admin_or_warehouse()`` /
``require_product_model_access()`` are called in the route rather than inside the
domain call. ``tools/extract_routes.py`` resolves guards by following the call
graph inside ``create_app`` and cannot see into another module, so a guard moved
down would make these routes read as merely "any authenticated" in the permission
matrix.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from traceability.auth import (
    current_operator_id,
    require_admin_or_warehouse,
    require_product_model_access,
)
from traceability.codes import batch_identification_code, make_qr_svg
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.idempotent_http import run_idempotent
from traceability.production_orders import (
    generate_production_order_for_po,
    production_order_data,
    production_order_progress_map,
    production_order_row,
)
from traceability.purchasing import (
    product_model_for_purchase_order,
    purchase_order_row,
)
from traceability.responses import success
from traceability.validators import business_id

production_orders_bp = Blueprint("production_orders", __name__)


@production_orders_bp.post("/api/production-orders")
def create_production_order():
    return run_idempotent("production-orders.create", _impl_create_production_order)


def _impl_create_production_order():
    require_admin_or_warehouse()
    payload = request.get_json(silent=True) or {}
    purchase_order_id = business_id(payload.get("purchaseOrderId"), "采购订单")
    external = bool(payload.get("external"))
    database = get_db()
    po = purchase_order_row(database, purchase_order_id)
    if not po:
        raise ApiError("采购订单不存在", 404)
    existing = database.execute(
        "SELECT id FROM production_orders WHERE purchase_order_id = ?",
        (purchase_order_id,),
    ).fetchone()
    if existing:
        raise ApiError("该采购订单已生成生产订单", 409)
    timestamp = current_app.config["NOW_PROVIDER"]()
    # Resolved and authorized here rather than inside the domain call, so the
    # product-scope guard stays visible to tools/extract_routes.py.
    product = product_model_for_purchase_order(database, po)
    require_product_model_access(product["id"])
    order_id = generate_production_order_for_po(
        database, po, product, external=external, timestamp=timestamp
    )
    return success(production_order_data(production_order_row(database, order_id)), 201)


@production_orders_bp.post("/api/production-orders/batch")
def create_production_orders_batch():
    return run_idempotent("production-orders.batch", _impl_create_production_orders_batch)


def _impl_create_production_orders_batch():
    # Warehouse batch-generates production orders (each minting its trace/QR
    # code) from the purchase orders operations submitted. Each PO is handled
    # in its own transaction so one failure does not abort the rest; orders
    # that already exist are reported as skipped rather than erroring.
    require_admin_or_warehouse()
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("purchaseOrderIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ApiError("请选择至少一个采购订单")
    external = bool(payload.get("external"))
    database = get_db()
    timestamp = current_app.config["NOW_PROVIDER"]()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in raw_ids:
        try:
            po_id = business_id(raw, "采购订单")
        except ApiError as error:
            failed.append({"purchaseOrderId": raw, "message": error.message})
            continue
        if po_id in seen:
            continue
        seen.add(po_id)
        po = purchase_order_row(database, po_id)
        if not po:
            failed.append({"purchaseOrderId": po_id, "message": "采购订单不存在"})
            continue
        existing = database.execute(
            "SELECT id FROM production_orders WHERE purchase_order_id = ?",
            (po_id,),
        ).fetchone()
        if existing:
            skipped.append(
                {
                    "purchaseOrderId": po_id,
                    "poNo": po["po_no"],
                    "productionOrderId": existing["id"],
                }
            )
            continue
        try:
            # Inside the try so a per-PO authorization failure is reported as a
            # skipped item rather than aborting the whole batch — the same
            # handling the guard got when it lived in the domain function.
            product = product_model_for_purchase_order(database, po)
            require_product_model_access(product["id"])
            order_id = generate_production_order_for_po(
                database, po, product, external=external, timestamp=timestamp
            )
        except ApiError as error:
            failed.append(
                {"purchaseOrderId": po_id, "poNo": po["po_no"], "message": error.message}
            )
            continue
        created.append(
            production_order_data(production_order_row(database, order_id))
        )
    return success(
        {"created": created, "skipped": skipped, "failed": failed}, 201
    )


@production_orders_bp.get("/api/production-orders")
def list_production_orders():
    require_admin_or_warehouse()
    clauses: list[str] = []
    parameters: list[Any] = []
    po_value = request.args.get("purchaseOrderId")
    if po_value not in {None, ""}:
        clauses.append("pro.purchase_order_id = ?")
        parameters.append(business_id(po_value, "采购订单"))
    operator_id = current_operator_id()
    if operator_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM user_product_model_permissions upp "
            "WHERE upp.user_id = ? AND upp.product_model_id = pb.product_model_id)"
        )
        parameters.append(operator_id)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    database = get_db()
    rows = database.execute(
        f"""
        SELECT pro.*, po.po_no, po.quantity, po.part_type_id,
               pb.batch_code, pb.product_model_id, pb.planned_quantity,
               pb.prefix, pb.generated_at, pm.model_code, pm.name AS product_name
        FROM production_orders pro
        JOIN purchase_orders po ON po.id = pro.purchase_order_id
        JOIN production_batches pb ON pb.id = pro.production_batch_id
        JOIN product_models pm ON pm.id = pb.product_model_id
        {where_clause}
        ORDER BY pro.created_at DESC, pro.id DESC
        """,
        parameters,
    ).fetchall()
    # Batched flow state (登记/质量/已入库) so the list shows where each order
    # stands without an extra query per row.
    progress = production_order_progress_map(database, list(rows))
    return success(
        [
            {**production_order_data(row), "progress": progress.get(row["id"], {})}
            for row in rows
        ]
    )


@production_orders_bp.get("/api/production-orders/<int:production_order_id>")
def get_production_order(production_order_id: int):
    require_admin_or_warehouse()
    row = production_order_row(get_db(), production_order_id)
    if not row:
        raise ApiError("生产订单不存在", 404)
    require_product_model_access(row["product_model_id"])
    return success(production_order_data(row))


@production_orders_bp.get("/api/production-orders/<int:production_order_id>/qr")
def production_order_qr(production_order_id: int):
    require_admin_or_warehouse()
    row = production_order_row(get_db(), production_order_id)
    if not row:
        raise ApiError("生产订单不存在", 404)
    require_product_model_access(row["product_model_id"])
    return Response(
        make_qr_svg(batch_identification_code(row["batch_code"])),
        mimetype="image/svg+xml",
    )

