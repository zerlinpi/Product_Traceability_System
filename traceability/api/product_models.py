"""Product models — the middle of the product tree.

A family groups models; a model is what actually gets a trace plan, a serial
prefix and an identity source (scanner, Bluetooth or manual entry). Everything
produced downstream — machines, code batches, trace records — hangs off a model,
which is why deletion is the strictest operation here: a model may only be
removed while nothing has been built from it.

Identity source is the load-bearing field. It decides how a finished unit's SN is
captured on the line, and ``BLUETOOTH`` additionally requires a notify UUID and a
name prefix for the collector to match against.
"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, request

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_operator_id,
    current_user,
    require_admin,
    require_operations,
)
from traceability.capabilities import ROLE_ADMIN
from traceability.codes import normalize_entity_code
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import product_model_dict
from traceability.validators import clean_ble_uuid, clean_text, now_iso, parse_bool

product_models_bp = Blueprint("product_models", __name__)

IDENTITY_SOURCES = {"BLUETOOTH", "SCANNER", "MANUAL"}

# The list and the update response return the same shape, including the two
# counts the model page shows.
_MODEL_WITH_COUNTS = """
    SELECT pm.*, pf.product_code AS product_family_code,
           pf.name AS product_family_name,
           (SELECT COUNT(*) FROM machines m
            WHERE m.product_model_id = pm.id) AS finished_product_count,
           (SELECT COUNT(*) FROM trace_plans tp
            WHERE tp.product_model_id = pm.id) AS trace_plan_count
    FROM product_models pm
    JOIN product_families pf ON pf.id = pm.product_family_id
"""


def _identity_source(value: object) -> str:
    source = str(value or "SCANNER").strip().upper()
    if source not in IDENTITY_SOURCES:
        raise ApiError("识别方式只能是蓝牙、扫码枪或手工录入")
    return source


def _bluetooth_prefix(bluetooth_prefix: str, identity_source: str, fallback: str) -> str:
    """A Bluetooth model with no prefix would be undiscoverable.

    Falling back to the serial prefix (or the model code) keeps the collector
    able to match the device name, which is how a scan gun picks the right model.
    """
    if identity_source == "BLUETOOTH" and not bluetooth_prefix:
        return fallback
    return bluetooth_prefix


@product_models_bp.get("/api/product-models")
def list_product_models():
    family_id = request.args.get("familyId", type=int)
    active_only = parse_bool(request.args.get("activeOnly"), False)
    operator_id = current_operator_id()
    rows = get_db().execute(
        _MODEL_WITH_COUNTS
        + """
        WHERE (? IS NULL OR pm.product_family_id = ?)
          AND (? = 0 OR (pm.active = 1 AND pf.active = 1))
          AND (? IS NULL OR EXISTS(
                SELECT 1 FROM user_product_model_permissions permission
                WHERE permission.user_id = ? AND permission.product_model_id = pm.id
          ))
        ORDER BY pf.product_code COLLATE NOCASE, pm.active DESC, pm.model_code COLLATE NOCASE
        """,
        (family_id, family_id, 1 if active_only else 0, operator_id, operator_id),
    ).fetchall()
    return success([product_model_dict(row) for row in rows])


@product_models_bp.post("/api/product-models")
def create_product_model():
    require_admin()
    payload = request.get_json(silent=True) or {}
    try:
        family_id = int(payload.get("productFamilyId"))
    except (TypeError, ValueError):
        raise ApiError("请选择产品分类") from None
    database = get_db()
    family = database.execute(
        "SELECT id FROM product_families WHERE id = ? AND active = 1", (family_id,)
    ).fetchone()
    if not family:
        raise ApiError("产品分类不存在或已停用")

    model_code = normalize_entity_code(payload.get("modelCode"), "产品型号编码")
    name = clean_text(payload.get("name"), "产品型号名称", required=True, max_length=100)
    serial_prefix = clean_text(
        payload.get("serialPrefix"), "序列号前缀", max_length=40
    ).upper()
    identity_source = _identity_source(payload.get("identitySource"))
    bluetooth_prefix = _bluetooth_prefix(
        clean_text(payload.get("bluetoothNamePrefix"), "蓝牙名称前缀", max_length=40).upper(),
        identity_source,
        serial_prefix or model_code,
    )
    bluetooth_service_uuid = clean_ble_uuid(
        payload.get("bluetoothServiceUuid"), "蓝牙服务 UUID"
    )
    bluetooth_notify_uuid = clean_ble_uuid(
        payload.get("bluetoothNotifyUuid"),
        "蓝牙通知 UUID",
        required=identity_source == "BLUETOOTH",
    )
    timestamp = now_iso()
    cursor = database.execute(
        """
        INSERT INTO product_models(
            product_family_id, model_code, name, serial_prefix,
            identity_source, bluetooth_name_prefix,
            bluetooth_service_uuid, bluetooth_notify_uuid,
            active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            family_id, model_code, name, serial_prefix,
            identity_source, bluetooth_prefix,
            bluetooth_service_uuid, bluetooth_notify_uuid,
            timestamp, timestamp,
        ),
    )
    # Re-read so the response carries the same counts as the list endpoint; a
    # brand new model has none.
    row = database.execute(
        """
        SELECT pm.*, pf.product_code AS product_family_code,
               pf.name AS product_family_name, 0 AS finished_product_count,
               0 AS trace_plan_count
        FROM product_models pm JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE pm.id = ?
        """,
        (cursor.lastrowid,),
    ).fetchone()
    return success(product_model_dict(row), 201)


@product_models_bp.put("/api/product-models/<int:model_id>")
def update_product_model(model_id: int):
    require_admin()
    payload = request.get_json(silent=True) or {}
    database = get_db()
    current = database.execute(
        "SELECT * FROM product_models WHERE id = ?", (model_id,)
    ).fetchone()
    if not current:
        raise ApiError("产品型号不存在", 404)
    try:
        family_id = int(payload.get("productFamilyId", current["product_family_id"]))
    except (TypeError, ValueError):
        raise ApiError("请选择产品分类") from None
    if not database.execute(
        "SELECT id FROM product_families WHERE id = ?", (family_id,)
    ).fetchone():
        raise ApiError("产品分类不存在")

    # Absent fields keep their stored value. ``model_code`` is intentionally not
    # editable: it is baked into the serial prefix and into already-printed codes.
    name = clean_text(
        payload.get("name", current["name"]), "产品型号名称", required=True, max_length=100
    )
    serial_prefix = clean_text(
        payload.get("serialPrefix", current["serial_prefix"]), "序列号前缀", max_length=40
    ).upper()
    identity_source = _identity_source(
        payload.get("identitySource", current["identity_source"])
    )
    bluetooth_prefix = _bluetooth_prefix(
        clean_text(
            payload.get("bluetoothNamePrefix", current["bluetooth_name_prefix"]),
            "蓝牙名称前缀",
            max_length=40,
        ).upper(),
        identity_source,
        serial_prefix or current["model_code"],
    )
    bluetooth_service_uuid = clean_ble_uuid(
        payload.get("bluetoothServiceUuid", current["bluetooth_service_uuid"]),
        "蓝牙服务 UUID",
    )
    bluetooth_notify_uuid = clean_ble_uuid(
        payload.get("bluetoothNotifyUuid", current["bluetooth_notify_uuid"]),
        "蓝牙通知 UUID",
        required=identity_source == "BLUETOOTH",
    )
    active = 1 if parse_bool(payload.get("active"), bool(current["active"])) else 0
    database.execute(
        """
        UPDATE product_models SET product_family_id = ?, name = ?, serial_prefix = ?,
            identity_source = ?, bluetooth_name_prefix = ?,
            bluetooth_service_uuid = ?, bluetooth_notify_uuid = ?,
            active = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            family_id, name, serial_prefix, identity_source,
            bluetooth_prefix, bluetooth_service_uuid, bluetooth_notify_uuid,
            active, now_iso(), model_id,
        ),
    )
    row = database.execute(
        _MODEL_WITH_COUNTS + "WHERE pm.id = ?", (model_id,)
    ).fetchone()
    return success(product_model_dict(row))


@product_models_bp.delete("/api/product-models/<int:model_id>")
def delete_product_model(model_id: int):
    require_operations()
    database = get_db()
    product = database.execute(
        "SELECT * FROM product_models WHERE id = ?", (model_id,)
    ).fetchone()
    if not product:
        raise ApiError("产品型号不存在", 404)

    # Operations may only delete what they registered; an admin may delete any.
    # This reads current_actor_id(), which returns the real user id — the record
    # ownership check elsewhere once used current_operator_id() and was inert.
    actor = current_user()
    actor_role = actor["role"] if actor else ""
    if actor_role != ROLE_ADMIN and product["created_by_user_id"] != current_actor_id():
        raise ApiError("只能删除自己创建的产品", 403)

    # Only an unused model may go: once a code set, a code batch or a machine
    # exists, the model is part of the traceability record and must be disabled
    # rather than deleted.
    code_set_count = database.execute(
        "SELECT COUNT(*) AS n FROM product_code_sets WHERE product_model_id = ?",
        (model_id,),
    ).fetchone()["n"]
    code_batch_count = database.execute(
        "SELECT COUNT(*) AS n FROM product_code_batches WHERE product_model_id = ?",
        (model_id,),
    ).fetchone()["n"]
    machine_count = database.execute(
        "SELECT COUNT(*) AS n FROM machines WHERE product_model_id = ?",
        (model_id,),
    ).fetchone()["n"]
    if code_set_count or code_batch_count or machine_count:
        raise ApiError("该产品已生成二维码或存在生产记录，无法删除；请先停用产品", 409)

    database.execute("BEGIN IMMEDIATE")
    try:
        record_audit_event(
            database,
            "PRODUCT_MODEL_DELETED",
            "PRODUCT_MODEL",
            product["model_code"],
            related_object_code=product["name"],
            payload={"productModelId": model_id},
        )
        # trace_plans reference product_models with ON DELETE RESTRICT, so remove
        # the BOM plans (their slots cascade) before deleting the model itself.
        # user_product_model_permissions cascade automatically.
        database.execute(
            "DELETE FROM trace_plans WHERE product_model_id = ?", (model_id,)
        )
        database.execute("DELETE FROM product_models WHERE id = ?", (model_id,))
        database.commit()
    except sqlite3.IntegrityError as error:
        database.rollback()
        raise ApiError("该产品仍被其他数据引用，无法删除", 409) from error
    except Exception:
        database.rollback()
        raise
    return success({"id": model_id, "modelCode": product["model_code"]})
