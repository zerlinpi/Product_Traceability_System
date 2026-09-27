"""Purchase orders — the domain behind /api/purchase-orders.

A purchase order is what operations raise with a supplier and then push to
Lingxing. The local record is the source of truth for the free-form 48-column
template fields and for the sync state; Lingxing holds the order itself.

Extracted from ``app.py`` so the HTTP layer can move to a blueprint. The split is
deliberate: this module holds the rules (what an order is, who may see it, what a
push means, how the factory-progress figures are derived) and
``traceability/api/purchase_orders.py`` holds the request/response handling.

Three things stay outside on purpose:
- ``traceability/lingxing.py``    the API client — HTTP, signing, tokens, retry
- ``traceability/lingxing_writes.py`` the push plumbing shared with inbound and
                                  inventory sync
- ``traceability/xlsx_export.py`` the Excel construction; this module only decides
                                  which values go in which column
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any

from flask import current_app

from traceability.audit_events import record_audit_event
from traceability.auth import current_actor_id, current_user
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
from traceability.validators import (
    business_id,
    business_quantity,
    clean_text,
    coerce_export_number,
)

__all__ = [
    "PURCHASE_ORDER_EXPORT_COLUMN_SET",
    "PURCHASE_ORDER_EXPORT_COLUMNS",
    "PURCHASE_ORDER_NUMERIC_COLUMNS",
    "clean_purchase_order_fields",
    "parse_purchase_order_input",
    "product_model_for_purchase_order",
    "purchase_order_data",
    "purchase_order_export_values",
    "purchase_order_lingxing_order_sn",
    "purchase_order_planned_quantity",
    "purchase_order_row",
    "purchase_order_summary",
    "push_purchase_order_record",
    "query_purchase_orders",
]


PURCHASE_ORDER_EXPORT_COLUMNS = [
    "标识号", "采购单号", "供应商", "联系人", "采购方", "联系方式", "结算方式",
    "预付比例", "结算账期", "结算描述", "支付方式", "含税", "费用分配方式",
    "采购币种", "当前汇率", "运费", "运费币种", "其他费用", "其他费用币种",
    "采购员", "质检类型", "单据备注", "颜色", "材质", "内含配件", "包装要求",
    "特殊要求", "HS海关编码", "交货周期（天数）", "采购仓库", "计划编号", "SKU",
    "店铺", "FNSKU", "是否赠品", "单箱数量", "箱数", "实际采购量", "含税单价",
    "税率", "预计到货时间", "产品备注", "更新报价", "内含配件(产品)",
    "包装要求(产品)", "特殊要求（规避专利）", "HS海关编码(产品)",
    "交货周期（天数）(产品)",
]

PURCHASE_ORDER_EXPORT_COLUMN_SET = set(PURCHASE_ORDER_EXPORT_COLUMNS)

# Columns whose values are numeric so the exported cell keeps a numeric type /
# format instead of being written as text.
PURCHASE_ORDER_NUMERIC_COLUMNS = {
    "预付比例", "结算账期", "当前汇率", "运费", "其他费用", "单箱数量", "箱数",
    "实际采购量", "含税单价", "税率",
}

def purchase_order_row(database: sqlite3.Connection, purchase_order_id: int):
    # LEFT JOINs so free-form orders (no supplier / part link) are still
    # returned; the product link is included for the "和产品挂钩" requirement.
    return database.execute(
        """
        SELECT po.*, s.supplier_code, s.name AS supplier_name,
               s.contact AS supplier_contact, s.phone AS supplier_phone,
               pt.part_code, pt.name AS part_name, pt.specification,
               pm.model_code AS product_model_code, pm.name AS product_model_name,
               (SELECT COALESCE(SUM(ir.quantity), 0) FROM inbound_receipts ir
                 WHERE ir.purchase_order_id = po.id) AS received_quantity,
               (SELECT MAX(ir.received_at) FROM inbound_receipts ir
                 WHERE ir.purchase_order_id = po.id) AS last_received_at
        FROM purchase_orders po
        LEFT JOIN suppliers s ON s.id = po.supplier_id
        LEFT JOIN part_types pt ON pt.id = po.part_type_id
        LEFT JOIN product_models pm ON pm.id = po.product_model_id
        WHERE po.id = ?
        """,
        (purchase_order_id,),
    ).fetchone()

def purchase_order_summary(row: sqlite3.Row, fields: dict[str, Any]) -> dict[str, Any]:
    """Flatten the fields the warehouse needs when receiving / producing.

    Everything is derived from what operations entered plus the recorded
    inbound receipts, so the production-order and supplier-receiving pages
    can show one consistent block: identity (SKU / 店铺 / FNSKU / 负责人),
    the dates, and the ordered vs actually-received quantity and amount.
    """
    keys = row.keys()

    def text(*candidates: Any) -> str:
        for candidate in candidates:
            if candidate not in (None, ""):
                return str(candidate).strip()
        return ""

    ordered_quantity = coerce_export_number(fields.get("实际采购量"))
    if ordered_quantity is None and row["quantity"] not in (None, ""):
        ordered_quantity = coerce_export_number(row["quantity"])
    unit_price = coerce_export_number(fields.get("含税单价"))
    if unit_price is None:
        unit_price = coerce_export_number(fields.get("单价"))
    received_quantity = (
        coerce_export_number(row["received_quantity"])
        if "received_quantity" in keys else None
    ) or 0

    def amount(quantity: Any) -> float | None:
        if unit_price is None or quantity in (None, ""):
            return None
        return round(float(unit_price) * float(quantity), 2)

    return {
        "sku": text(
            fields.get("SKU"),
            row["product_model_code"] if "product_model_code" in keys else None,
            row["part_code"] if "part_code" in keys else None,
        ),
        "productName": text(
            row["product_model_name"] if "product_model_name" in keys else None,
            fields.get("品名"),
            row["part_name"] if "part_name" in keys else None,
        ),
        "shop": text(fields.get("店铺")),
        "fnsku": text(fields.get("FNSKU")),
        # 负责人: the account that placed the order, falling back to the
        # 采购员 typed on the template.
        "owner": text(row["created_by"], fields.get("采购员")),
        "supplierName": text(
            fields.get("供应商"),
            row["supplier_name"] if "supplier_name" in keys else None,
        ),
        "orderedAt": row["created_at"],
        "plannedArrivalAt": text(fields.get("预计到货时间")),
        "actualArrivalAt": (
            row["last_received_at"] if "last_received_at" in keys else None
        ),
        "unitPrice": float(unit_price) if unit_price is not None else None,
        "orderedQuantity": (
            float(ordered_quantity) if ordered_quantity is not None else None
        ),
        "receivedQuantity": float(received_quantity),
        "orderAmount": amount(ordered_quantity),
        "receivedAmount": amount(received_quantity),
    }

def purchase_order_data(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    try:
        fields = json.loads(row["fields_json"] or "{}") if "fields_json" in keys else {}
    except (json.JSONDecodeError, TypeError):
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    return {
        "summary": purchase_order_summary(row, fields),
        "id": row["id"],
        "poNo": row["po_no"],
        "supplierId": row["supplier_id"],
        "supplierCode": row["supplier_code"] if "supplier_code" in keys else None,
        "supplierName": row["supplier_name"] if "supplier_name" in keys else None,
        "supplierContact": row["supplier_contact"] if "supplier_contact" in keys else None,
        "supplierPhone": row["supplier_phone"] if "supplier_phone" in keys else None,
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"] if "part_code" in keys else None,
        "partName": row["part_name"] if "part_name" in keys else None,
        "specification": row["specification"] if "specification" in keys else None,
        "productModelId": row["product_model_id"] if "product_model_id" in keys else None,
        "productModelCode": row["product_model_code"] if "product_model_code" in keys else None,
        "productModelName": row["product_model_name"] if "product_model_name" in keys else None,
        "quantity": row["quantity"],
        "fields": fields,
        "syncStatus": row["sync_status"],
        "pushInProgress": bool(row["push_in_progress"]),
        "lingxingPoId": row["lingxing_po_id"] or None,
        "pushError": row["push_error"],
        "createdBy": row["created_by"],
        "createdByUserId": row["created_by_user_id"],
        "createdAt": row["created_at"],
        "pushedAt": row["pushed_at"],
    }

def clean_purchase_order_fields(raw_fields: object) -> dict[str, Any]:
    """Normalize the free-form template fields entered by operations.

    Values are keyed by the export column name; unknown columns are dropped
    so the stored payload always maps onto the export template. Numbers and
    booleans are kept as-is; everything else is trimmed text capped at 500
    characters.
    """
    fields: dict[str, Any] = {}
    if raw_fields is None:
        return fields
    if not isinstance(raw_fields, dict):
        raise ApiError("采购单字段格式无效")
    for key, value in raw_fields.items():
        column = str(key).strip()
        if not column or column not in PURCHASE_ORDER_EXPORT_COLUMN_SET:
            continue
        if value is None:
            fields[column] = ""
        elif isinstance(value, (bool, int, float)):
            fields[column] = value
        else:
            text = str(value).strip()
            if len(text) > 500:
                raise ApiError(f"字段“{column}”内容不能超过 500 个字符")
            fields[column] = text
    return fields

def parse_purchase_order_input(
    database: sqlite3.Connection, payload: dict[str, Any], actor_role: str
) -> tuple[int | None, int | None, int | None, int | None, dict[str, Any]]:
    """Validate and normalize purchase-order input shared by create/update.

    Returns ``(product_model_id, supplier_id, part_type_id, quantity,
    fields)``. Supplier / 商品 links are optional; operations may only link
    products they created.
    """
    fields = clean_purchase_order_fields(payload.get("fields"))

    product_model_id: int | None = None
    raw_product = payload.get("productModelId")
    if raw_product not in (None, ""):
        product_model_id = business_id(raw_product, "产品")
        product = database.execute(
            "SELECT id, created_by_user_id FROM product_models WHERE id = ?",
            (product_model_id,),
        ).fetchone()
        if not product:
            raise ApiError("产品不存在", 404)
        if actor_role == "OPERATIONS" and product["created_by_user_id"] != current_actor_id():
            raise ApiError("只能选择自己创建的产品", 403)

    supplier_id: int | None = None
    raw_supplier = payload.get("supplierId")
    if raw_supplier not in (None, ""):
        supplier_id = business_id(raw_supplier, "供应商")
        if not database.execute(
            "SELECT id FROM suppliers WHERE id = ? AND active = 1", (supplier_id,)
        ).fetchone():
            raise ApiError("供应商不存在或已停用", 404)
    part_type_id: int | None = None
    raw_part = payload.get("partTypeId")
    if raw_part not in (None, ""):
        part_type_id = business_id(raw_part, "商品")
        part = database.execute(
            "SELECT id, supplier_id FROM part_types WHERE id = ? AND active = 1",
            (part_type_id,),
        ).fetchone()
        if not part:
            raise ApiError("商品不存在或已停用", 404)
        if supplier_id is not None and int(part["supplier_id"]) != supplier_id:
            raise ApiError("所选商品不属于该供应商", 409)

    quantity: int | None = None
    raw_quantity = payload.get("quantity")
    if raw_quantity not in (None, ""):
        quantity = business_quantity(raw_quantity, "采购数量")
    if quantity is not None and "实际采购量" not in fields:
        fields["实际采购量"] = quantity

    return product_model_id, supplier_id, part_type_id, quantity, fields

def query_purchase_orders(
    *,
    sync_status: object = None,
    supplier_id: object = None,
    created_from: object = None,
    created_to: object = None,
) -> list[sqlite3.Row]:
    """List/export query. Filters arrive as arguments, not read from ``request``.

    Read access is shared with warehouse so it can select a source order;
    operations only ever see their own orders. The route pulls the query string
    apart and passes the raw values in, so this module stays free of Flask and
    the validation order below is the only order callers see.
    """
    clauses: list[str] = []
    parameters: list[Any] = []
    status = clean_text(sync_status, "同步状态", max_length=16).upper()
    if status:
        if status not in {"PENDING", "PUSHED", "FAILED"}:
            raise ApiError("同步状态无效")
        clauses.append("po.sync_status = ?")
        parameters.append(status)
    if supplier_id not in {None, ""}:
        clauses.append("po.supplier_id = ?")
        parameters.append(business_id(supplier_id, "供应商"))
    range_from = clean_text(created_from, "起始时间", max_length=64)
    range_to = clean_text(created_to, "结束时间", max_length=64)
    if range_from and range_to and range_from > range_to:
        raise ApiError("起始时间不能晚于结束时间")
    if range_from:
        clauses.append("po.created_at >= ?")
        parameters.append(range_from)
    if range_to:
        clauses.append("po.created_at <= ?")
        parameters.append(range_to)
    actor = current_user()
    if actor and actor["role"] == "OPERATIONS":
        clauses.append("po.created_by_user_id = ?")
        parameters.append(current_actor_id())
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return get_db().execute(
        f"""
        SELECT po.*, s.supplier_code, s.name AS supplier_name,
               s.contact AS supplier_contact, s.phone AS supplier_phone,
               pt.part_code, pt.name AS part_name, pt.specification,
               pm.model_code AS product_model_code, pm.name AS product_model_name,
               (SELECT COALESCE(SUM(ir.quantity), 0) FROM inbound_receipts ir
                 WHERE ir.purchase_order_id = po.id) AS received_quantity,
               (SELECT MAX(ir.received_at) FROM inbound_receipts ir
                 WHERE ir.purchase_order_id = po.id) AS last_received_at
        FROM purchase_orders po
        LEFT JOIN suppliers s ON s.id = po.supplier_id
        LEFT JOIN part_types pt ON pt.id = po.part_type_id
        LEFT JOIN product_models pm ON pm.id = po.product_model_id
        {where_clause}
        ORDER BY po.created_at DESC, po.id DESC
        """,
        parameters,
    ).fetchall()

def purchase_order_lingxing_order_sn(
    row: sqlite3.Row,
    fields: dict[str, Any],
    requested: object = None,
) -> str:
    """Resolve the Lingxing 采购单号 (``order_sn``) this local order maps to.

    采购单下单 acts on an order that already exists in Lingxing, so the push
    needs that order's number rather than any local id. Callers may pass it
    explicitly; otherwise a previously recorded number is reused, then the
    采购单号 typed on the template, and finally our own document number
    (which is what the export writes into Lingxing).
    """
    for candidate in (
        requested,
        row["lingxing_po_id"] if "lingxing_po_id" in row.keys() else "",
        fields.get("采购单号"),
        row["po_no"],
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        if len(text) > 64:
            raise ApiError("领星采购单号不能超过 64 个字符")
        return text
    raise ApiError("缺少领星采购单号，无法执行采购单下单")

def push_purchase_order_record(
    purchase_order_id: int,
    requested_order_sn: object = None,
) -> dict[str, Any]:
    """Push one local order to Lingxing.

    Authorization is the caller's job — the route calls ``require_operations()``
    before this runs. Keeping the guard out of here is deliberate: it is what
    lets ``tools/extract_routes.py`` see the guard when it builds the permission
    matrix, since that tool follows the call graph inside ``create_app`` and
    cannot see into this module. A guard hidden in here would make the matrix
    under-report the route as merely "any authenticated".
    """
    database = get_db()
    row = purchase_order_row(database, purchase_order_id)
    if not row:
        raise ApiError("采购订单不存在", 404)
    if row["sync_status"] == "PUSHED":
        return {**purchase_order_data(row), "message": "该采购订单已推送"}
    service = lingxing_service(database)
    ensure_lingxing_operation_ready(service, "purchase_order")
    try:
        po_fields = json.loads(row["fields_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        po_fields = {}
    if not isinstance(po_fields, dict):
        po_fields = {}
    # Resolved before the in-progress guard so a missing/invalid order_sn
    # never leaves the record flagged as pushing.
    order_sn = purchase_order_lingxing_order_sn(row, po_fields, requested_order_sn)
    database.execute("BEGIN IMMEDIATE")
    try:
        current = database.execute(
            "SELECT sync_status, push_in_progress, push_started_at "
            "FROM purchase_orders WHERE id = ?",
            (purchase_order_id,),
        ).fetchone()
        if current["sync_status"] == "PUSHED":
            database.rollback()
            return {**purchase_order_data(purchase_order_row(database, purchase_order_id)), "message": "该采购订单已推送"}
        guard_started_at = current_app.config["NOW_PROVIDER"]()
        if current["push_in_progress"] and not guard_is_stale(
            current["push_started_at"], guard_started_at, PUSH_GUARD_TIMEOUT
        ):
            raise ApiError("采购订单推送正在进行中", 409)
        # Stamping the start lets a later attempt recover an abandoned guard.
        database.execute(
            "UPDATE purchase_orders SET push_in_progress = 1, push_started_at = ? "
            "WHERE id = ?",
            (guard_started_at, purchase_order_id),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise

    try:
        # 采购单下单 (``setOrders``) moves an existing Lingxing purchase order
        # from 待下单 to 待到货. It is keyed by the Lingxing 采购单号 and answers
        # with an empty ``data`` list, so no object id comes back and the
        # order_sn we sent is what we persist.
        response = service.push("purchase_order", {"order_sn": [order_sn]})
        lingxing_id = (
            external_identifier(response, "poId", "purchaseOrderId", "order_sn", "id")
            or order_sn
        )
        timestamp = current_app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            UPDATE purchase_orders
            SET sync_status = 'PUSHED', push_in_progress = 0,
                lingxing_po_id = ?, lingxing_raw_response = ?,
                push_error = '', pushed_at = ?
            WHERE id = ?
            """,
            (lingxing_id, json.dumps(response, ensure_ascii=False), timestamp, purchase_order_id),
        )
        record_audit_event(
            database,
            "PO_PUSHED",
            "PURCHASE_ORDER",
            row["po_no"],
            payload={
                "result": "PUSHED",
                "lingxingPoId": lingxing_id,
                "orderSn": order_sn,
            },
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
            UPDATE purchase_orders
            SET sync_status = 'FAILED', push_in_progress = 0, push_error = ?
            WHERE id = ?
            """,
            (message, purchase_order_id),
        )
        record_audit_event(
            database,
            "PO_PUSH_FAILED",
            "PURCHASE_ORDER",
            row["po_no"],
            reason=message,
            payload={"result": "FAILED", "error": message},
            occurred_at=timestamp,
        )
        database.commit()
        if isinstance(error, (ApiError, LingxingError)):
            raise
        raise LingxingError(f"领星采购订单推送失败：{message}") from error
    return {**purchase_order_data(purchase_order_row(database, purchase_order_id)), "message": "采购订单已推送"}

def purchase_order_planned_quantity(po: sqlite3.Row) -> int:
    """Resolve how many units a purchase order plans to produce.

    Free-form orders keep their quantity in the template field 实际采购量
    rather than the legacy ``quantity`` column, so fall back to it (and
    finally to 1) instead of inserting NULL into ``planned_quantity``.
    """
    candidates: list[Any] = []
    keys = po.keys()
    if "quantity" in keys:
        candidates.append(po["quantity"])
    if "fields_json" in keys:
        try:
            fields = json.loads(po["fields_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            fields = {}
        if isinstance(fields, dict):
            candidates.append(fields.get("实际采购量"))
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            quantity = int(float(str(candidate).strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= quantity <= 999999:
            return quantity
    return 1

def product_model_for_purchase_order(database: sqlite3.Connection, po: sqlite3.Row):
    # Prefer the explicit product link when present; fall back to resolving
    # the product via the legacy 商品(part) -> active trace plan mapping.
    keys = po.keys()
    product_model_id = po["product_model_id"] if "product_model_id" in keys else None
    if product_model_id is not None:
        # A BOM (trace plan) is optional: a product may be a complete,
        # indivisible finished good. The plan is only carried onto the batch
        # when it exists, so component-less products still get a production
        # order and its unique QR code.
        row = database.execute(
            """
            SELECT pm.*, (
                SELECT tp.id FROM trace_plans tp
                WHERE tp.product_model_id = pm.id AND tp.status = 'ACTIVE'
                ORDER BY tp.activated_at DESC, tp.id DESC LIMIT 1
            ) AS trace_plan_id
            FROM product_models pm
            WHERE pm.id = ? AND pm.active = 1
            """,
            (product_model_id,),
        ).fetchone()
        if not row:
            raise ApiError("采购订单关联的产品不存在或已停用", 409)
        return row
    if po["part_type_id"] is None:
        raise ApiError("采购订单未关联可生产的产品", 409)
    rows = database.execute(
        """
        SELECT DISTINCT pm.*, tp.id AS trace_plan_id
        FROM trace_plan_slots slot
        JOIN trace_plans tp ON tp.id = slot.trace_plan_id AND tp.status = 'ACTIVE'
        JOIN product_models pm ON pm.id = tp.product_model_id AND pm.active = 1
        WHERE slot.part_type_id = ?
        ORDER BY tp.activated_at DESC, tp.id DESC
        """,
        (po["part_type_id"],),
    ).fetchall()
    if not rows:
        raise ApiError("采购商品尚未关联可生产的产品型号与生产计划", 409)
    if len({int(row["id"]) for row in rows}) > 1:
        raise ApiError("采购商品关联了多个产品型号，请先明确产品生产计划", 409)
    return rows[0]

def purchase_order_export_values(row: sqlite3.Row) -> tuple[dict[str, Any], bool]:
    """Build the exact template column -> value map for one purchase order.

    Values come from what operations entered; legacy supplier/part-linked
    orders still derive their classic defaults so historical exports are
    unchanged. Returns ``(values, is_legacy_linked)`` and never raises, so
    the batch export can include every order.
    """
    try:
        stored_fields = json.loads(row["fields_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        stored_fields = {}
    if not isinstance(stored_fields, dict):
        stored_fields = {}

    values: dict[str, Any] = dict.fromkeys(PURCHASE_ORDER_EXPORT_COLUMNS, "")
    values["标识号"] = row["id"]
    values["采购单号"] = row["po_no"]
    values["采购方"] = "聚星同创仓库管理系统"
    if row["created_by"]:
        values["采购员"] = row["created_by"]

    is_legacy_linked = row["part_type_id"] is not None or row["supplier_id"] is not None
    if is_legacy_linked:
        for key, derived in {
            "供应商": row["supplier_name"],
            "含税": "是",
            "费用分配方式": "按数量",
            "采购币种": "CNY",
            "采购仓库": "默认仓库",
            "SKU": row["part_code"],
            "实际采购量": row["quantity"],
            "含税单价": Decimal("1.00"),
            "联系人": row["supplier_contact"],
            "联系方式": row["supplier_phone"],
            "产品备注": row["part_name"],
        }.items():
            if derived not in (None, ""):
                values[key] = derived
    if row["product_model_name"] and not values.get("产品备注"):
        values["产品备注"] = row["product_model_name"]

    # Operator-entered template values take precedence over derived defaults.
    for column, value in stored_fields.items():
        if column not in values or value in (None, ""):
            continue
        if column in PURCHASE_ORDER_NUMERIC_COLUMNS:
            number = coerce_export_number(value)
            if number is not None:
                value = number
        if column == "含税单价" and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = Decimal(str(value))
        values[column] = value
    return values, is_legacy_linked
