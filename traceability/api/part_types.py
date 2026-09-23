"""Supplier part types — the purchasable components.

A part type belongs to a supplier and carries the stock floor used by the
low-stock view. Only an administrator may change one: ``part_code`` is the
identifier printed on labels and stored in trace plans, so a careless edit would
break the link between a physical part and its traceability record.

The ``category_code`` / ``category_name`` pair is kept consistent across every
part type sharing a code — creating one under an existing code adopts that code's
name rather than forking a second spelling.
"""

from __future__ import annotations

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import current_operator_id, require_admin
from traceability.codes import normalize_entity_code
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import part_type_dict
from traceability.validators import clean_text, parse_bool

part_types_bp = Blueprint("part_types", __name__)

MAXIMUM_MINIMUM_STOCK = 10_000_000


def _clean_minimum_stock(value: object) -> int:
    try:
        minimum_stock = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ApiError("安全库存必须是整数") from error
    if not 0 <= minimum_stock <= MAXIMUM_MINIMUM_STOCK:
        raise ApiError(f"安全库存需在 0-{MAXIMUM_MINIMUM_STOCK} 之间")
    return minimum_stock


@part_types_bp.get("/api/part-types")
def list_part_types():
    operator_id = current_operator_id()
    rows = get_db().execute(
        """
        SELECT pt.*, s.supplier_code, s.name AS supplier_name,
               COUNT(sib.id) AS batch_count,
               COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
        FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
        WHERE (? IS NULL OR EXISTS(
            SELECT 1 FROM user_supplier_permissions permission
            WHERE permission.user_id = ? AND permission.supplier_id = pt.supplier_id
        ))
        GROUP BY pt.id
        ORDER BY pt.created_at DESC, pt.id DESC
        """,
        (operator_id, operator_id),
    ).fetchall()
    return success([part_type_dict(row) for row in rows])


@part_types_bp.post("/api/part-types")
def create_part_type():
    require_admin()
    payload = request.get_json(silent=True) or {}
    part_code = normalize_entity_code(payload.get("partCode"), "部件编码")
    name = clean_text(payload.get("name"), "部件名称", required=True, max_length=100)
    category_code = normalize_entity_code(
        payload.get("categoryCode") or part_code, "部件分类编码"
    )
    category_name = clean_text(
        payload.get("categoryName") or name, "部件分类名称", required=True, max_length=100
    )
    specification = clean_text(payload.get("specification"), "规格型号", max_length=150)
    minimum_stock = _clean_minimum_stock(payload.get("minimumStock", 0))

    try:
        supplier_id = int(payload.get("supplierId"))
    except (TypeError, ValueError):
        raise ApiError("请选择供应商") from None
    database = get_db()
    supplier = database.execute(
        "SELECT id FROM suppliers WHERE id = ?", (supplier_id,)
    ).fetchone()
    if not supplier:
        raise ApiError("供应商不存在")

    # One category code, one name: reuse whatever is already stored rather than
    # letting two spellings of the same category appear in the picker.
    existing_category = database.execute(
        "SELECT category_name FROM part_types WHERE category_code = ? LIMIT 1",
        (category_code,),
    ).fetchone()
    if existing_category:
        category_name = existing_category["category_name"]

    timestamp = current_app.config["NOW_PROVIDER"]()
    cursor = database.execute(
        """
        INSERT INTO part_types(
            part_code, name, category_code, category_name,
            specification, minimum_stock, supplier_id, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            part_code, name, category_code, category_name,
            specification, minimum_stock, supplier_id, timestamp, timestamp,
        ),
    )
    row = database.execute(
        """
        SELECT pt.*, s.supplier_code, s.name AS supplier_name
        FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id WHERE pt.id = ?
        """,
        (cursor.lastrowid,),
    ).fetchone()
    return success(part_type_dict(row), 201)


@part_types_bp.put("/api/part-types/<int:part_type_id>")
def update_part_type(part_type_id: int):
    require_admin()
    database = get_db()
    current = database.execute(
        "SELECT * FROM part_types WHERE id = ?", (part_type_id,)
    ).fetchone()
    if not current:
        raise ApiError("供应部件不存在", 404)
    payload = request.get_json(silent=True) or {}
    # Absent fields keep their stored value.
    part_code = normalize_entity_code(
        payload.get("partCode", current["part_code"]), "部件编码"
    )
    name = clean_text(
        payload.get("name", current["name"]), "部件名称", required=True, max_length=100
    )
    specification = clean_text(
        payload.get("specification", current["specification"]), "规格型号", max_length=150
    )
    minimum_stock = _clean_minimum_stock(payload.get("minimumStock", current["minimum_stock"]))
    active = parse_bool(payload.get("active"), bool(current["active"]))
    timestamp = current_app.config["NOW_PROVIDER"]()
    database.execute(
        """
        UPDATE part_types
        SET part_code = ?, name = ?, category_code = ?, category_name = ?,
            specification = ?, minimum_stock = ?, active = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            part_code, name, part_code, name, specification, minimum_stock,
            int(active), timestamp, part_type_id,
        ),
    )
    record_audit_event(
        database,
        "SUPPLIER_PART_UPDATED",
        "PART_TYPE",
        part_code,
        payload={"name": name, "active": active, "minimumStock": minimum_stock},
        occurred_at=timestamp,
    )
    row = database.execute(
        """
        SELECT pt.*, s.supplier_code, s.name AS supplier_name
        FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id
        WHERE pt.id = ?
        """,
        (part_type_id,),
    ).fetchone()
    return success(part_type_dict(row))
