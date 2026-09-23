"""Product families — the top level of the product tree.

A family (走步机, 床架, ...) groups product models; models carry the trace plan
and the batches. Only an administrator may create or edit one, because the family
code is what the treadmill-specific batch rules key off
(see ``TREADMILL_FAMILY_CODE``) and a rename would move products between rules.
"""

from __future__ import annotations

from flask import Blueprint, request

from traceability.auth import current_operator_id, require_admin
from traceability.codes import normalize_entity_code
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import product_family_dict
from traceability.validators import clean_text, now_iso, parse_bool

product_families_bp = Blueprint("product_families", __name__)

# ``model_count`` is computed in the query rather than by a second round trip, so
# the list endpoint stays one statement per request.
_MODEL_COUNT = """
    (SELECT COUNT(*) FROM product_models pm
     WHERE pm.product_family_id = pf.id) AS model_count
"""


@product_families_bp.get("/api/product-families")
def list_product_families():
    operator_id = current_operator_id()
    rows = get_db().execute(
        f"""
        SELECT pf.*, {_MODEL_COUNT}
        FROM product_families pf
        WHERE (? IS NULL OR EXISTS(
            SELECT 1
            FROM product_models scoped_model
            JOIN user_product_model_permissions permission
              ON permission.product_model_id = scoped_model.id
            WHERE permission.user_id = ?
              AND scoped_model.product_family_id = pf.id
        ))
        ORDER BY pf.active DESC, pf.product_code COLLATE NOCASE
        """,
        (operator_id, operator_id),
    ).fetchall()
    return success([product_family_dict(row) for row in rows])


@product_families_bp.post("/api/product-families")
def create_product_family():
    require_admin()
    payload = request.get_json(silent=True) or {}
    product_code = normalize_entity_code(payload.get("productCode"), "产品分类编码")
    name = clean_text(payload.get("name"), "产品分类名称", required=True, max_length=80)
    description = clean_text(payload.get("description"), "产品分类说明", max_length=300)
    timestamp = now_iso()
    cursor = get_db().execute(
        """
        INSERT INTO product_families(
            product_code, name, description, active, created_at, updated_at
        ) VALUES (?, ?, ?, 1, ?, ?)
        """,
        (product_code, name, description, timestamp, timestamp),
    )
    # Re-read so the response carries the same shape as the list endpoint
    # (``model_count`` included); a new family has none.
    row = get_db().execute(
        f"SELECT pf.*, {_MODEL_COUNT} FROM product_families pf WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return success(product_family_dict(row), 201)


@product_families_bp.put("/api/product-families/<int:family_id>")
def update_product_family(family_id: int):
    require_admin()
    payload = request.get_json(silent=True) or {}
    database = get_db()
    current = database.execute(
        "SELECT * FROM product_families WHERE id = ?", (family_id,)
    ).fetchone()
    if not current:
        raise ApiError("产品分类不存在", 404)
    # Absent fields keep their stored value, so a partial update cannot blank a
    # name the operator did not touch.
    name = clean_text(
        payload.get("name", current["name"]), "产品分类名称", required=True, max_length=80
    )
    description = clean_text(
        payload.get("description", current["description"]), "产品分类说明", max_length=300
    )
    active = 1 if parse_bool(payload.get("active"), bool(current["active"])) else 0
    database.execute(
        "UPDATE product_families SET name = ?, description = ?, active = ?, updated_at = ? "
        "WHERE id = ?",
        (name, description, active, now_iso(), family_id),
    )
    row = database.execute(
        f"SELECT pf.*, {_MODEL_COUNT} FROM product_families pf WHERE pf.id = ?",
        (family_id,),
    ).fetchone()
    return success(product_family_dict(row))
