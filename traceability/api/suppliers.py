"""Suppliers and their detail view.

Suppliers are the root of the purchasing half of the traceability chain: a
supplier owns part types, part types receive inventory batches, and batches are
consumed by production batches. That is why the detail view is the heaviest
endpoint here — it answers "what does this supplier feed into?", which is the
question asked when a supplier is suspended or a part is found defective.

Only an administrator may create or edit one.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import current_operator_id, require_admin
from traceability.codes import normalize_entity_code
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import (
    part_type_dict,
    supplier_dict,
    supplier_inventory_batch_dict,
)
from traceability.validators import clean_text, parse_bool

suppliers_bp = Blueprint("suppliers", __name__)

# Rolled-up counts used by both the list and the detail view. COUNT(DISTINCT ...)
# because the two LEFT JOINs multiply rows against each other.
_SUPPLIER_ROLLUP = """
    SELECT s.*,
           COUNT(DISTINCT pt.id) AS part_count,
           COUNT(DISTINCT sib.id) AS batch_count,
           COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
    FROM suppliers s
    LEFT JOIN part_types pt ON pt.supplier_id = s.id
    LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
"""


@suppliers_bp.get("/api/suppliers")
def list_suppliers():
    operator_id = current_operator_id()
    rows = get_db().execute(
        _SUPPLIER_ROLLUP
        + """
        WHERE (? IS NULL OR EXISTS(
            SELECT 1 FROM user_supplier_permissions permission
            WHERE permission.user_id = ? AND permission.supplier_id = s.id
        ))
        GROUP BY s.id
        ORDER BY s.created_at DESC, s.id DESC
        """,
        (operator_id, operator_id),
    ).fetchall()
    return success([supplier_dict(row) for row in rows])


@suppliers_bp.post("/api/suppliers")
def create_supplier():
    require_admin()
    payload = request.get_json(silent=True) or {}
    code = normalize_entity_code(payload.get("supplierCode"), "供应商编码")
    name = clean_text(payload.get("name"), "供应商名称", required=True, max_length=100)
    contact = clean_text(payload.get("contact"), "联系人", max_length=50)
    phone = clean_text(payload.get("phone"), "联系电话", max_length=50)
    database = get_db()
    timestamp = current_app.config["NOW_PROVIDER"]()
    cursor = database.execute(
        """
        INSERT INTO suppliers(
            supplier_code, name, contact, phone, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (code, name, contact, phone, timestamp, timestamp),
    )
    row = database.execute(
        "SELECT * FROM suppliers WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return success(supplier_dict(row), 201)


@suppliers_bp.put("/api/suppliers/<int:supplier_id>")
def update_supplier(supplier_id: int):
    require_admin()
    database = get_db()
    current = database.execute(
        "SELECT * FROM suppliers WHERE id = ?", (supplier_id,)
    ).fetchone()
    if not current:
        raise ApiError("供应商不存在", 404)
    payload = request.get_json(silent=True) or {}
    # Absent fields keep their stored value.
    code = normalize_entity_code(
        payload.get("supplierCode", current["supplier_code"]), "供应商编码"
    )
    name = clean_text(
        payload.get("name", current["name"]), "供应商名称", required=True, max_length=100
    )
    contact = clean_text(payload.get("contact", current["contact"]), "联系人", max_length=50)
    phone = clean_text(payload.get("phone", current["phone"]), "联系电话", max_length=50)
    active = parse_bool(payload.get("active"), bool(current["active"]))
    timestamp = current_app.config["NOW_PROVIDER"]()
    database.execute(
        """
        UPDATE suppliers
        SET supplier_code = ?, name = ?, contact = ?, phone = ?, active = ?, updated_at = ?
        WHERE id = ?
        """,
        (code, name, contact, phone, int(active), timestamp, supplier_id),
    )
    record_audit_event(
        database,
        "SUPPLIER_UPDATED",
        "SUPPLIER",
        code,
        payload={"name": name, "active": active},
        occurred_at=timestamp,
    )
    row = database.execute(
        "SELECT * FROM suppliers WHERE id = ?", (supplier_id,)
    ).fetchone()
    return success(supplier_dict(row))


@suppliers_bp.get("/api/suppliers/<int:supplier_id>")
def get_supplier_detail(supplier_id: int):
    require_admin()
    database = get_db()
    supplier = database.execute(
        _SUPPLIER_ROLLUP + " WHERE s.id = ? GROUP BY s.id",
        (supplier_id,),
    ).fetchone()
    if not supplier:
        raise ApiError("供应商不存在", 404)

    part_rows = database.execute(
        """
        SELECT pt.*, s.supplier_code, s.name AS supplier_name,
               COUNT(sib.id) AS batch_count,
               COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
        FROM part_types pt
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
        WHERE pt.supplier_id = ?
        GROUP BY pt.id
        ORDER BY pt.active DESC, pt.created_at DESC, pt.id DESC
        """,
        (supplier_id,),
    ).fetchall()

    batch_rows = database.execute(
        """
        SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
               s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
        FROM supplier_inventory_batches sib
        JOIN part_types pt ON pt.id = sib.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        WHERE pt.supplier_id = ?
        ORDER BY sib.active DESC, sib.received_date DESC, sib.id DESC
        """,
        (supplier_id,),
    ).fetchall()

    # Which active trace plans consume this supplier's parts, grouped by product
    # model — the reverse view that answers "what breaks if this supplier stops?".
    usage_rows = database.execute(
        """
        SELECT pm.id AS product_model_id, pm.model_code AS product_code,
               pm.name AS product_name, pm.active AS product_active,
               tp.id AS trace_plan_id, pt.id AS part_type_id,
               pt.part_code, pt.name AS part_name,
               COUNT(tps.id) AS required_quantity,
               (SELECT COUNT(*) FROM product_code_sets pcs
                WHERE pcs.product_model_id = pm.id) AS generated_count
        FROM trace_plan_slots tps
        JOIN trace_plans tp ON tp.id = tps.trace_plan_id
        JOIN product_models pm ON pm.id = tp.product_model_id
        JOIN part_types pt ON pt.id = tps.part_type_id
        WHERE pt.supplier_id = ? AND tp.status = 'ACTIVE'
        GROUP BY pm.id, tp.id, pt.id
        ORDER BY pm.active DESC, pm.name COLLATE NOCASE, pt.name COLLATE NOCASE
        """,
        (supplier_id,),
    ).fetchall()
    used_in_products: dict[int, dict[str, Any]] = {}
    for row in usage_rows:
        item = used_in_products.setdefault(
            row["product_model_id"],
            {
                "id": row["product_model_id"],
                "productCode": row["product_code"],
                "name": row["product_name"],
                "active": bool(row["product_active"]),
                "tracePlanId": row["trace_plan_id"],
                "generatedCount": row["generated_count"],
                "requiredQuantity": 0,
                "parts": [],
            },
        )
        item["requiredQuantity"] += row["required_quantity"]
        item["parts"].append(
            {
                "id": row["part_type_id"],
                "partCode": row["part_code"],
                "name": row["part_name"],
                "requiredQuantity": row["required_quantity"],
            }
        )
    return success(
        {
            "supplier": supplier_dict(supplier),
            "parts": [part_type_dict(row) for row in part_rows],
            "batches": [supplier_inventory_batch_dict(row) for row in batch_rows],
            "usedInProducts": list(used_in_products.values()),
        }
    )
