"""Bluetooth device discovery and SN collection.

These three endpoints are the whole Bluetooth surface. They were the first
routes moved out of ``app.py`` because they are self-contained: they touch the
BLE collector, the product-model table and nothing else, so they were the
cheapest place to prove the blueprint pattern end to end.

All three require only authentication, not a role — an operator scanning a
device is doing their own job, and the per-model permission is applied inside the
query (``user_product_model_permissions``) rather than by a decorator. That is
unchanged from before the move; see the ``/api/bluetooth/*`` rows in
``docs/PERMISSION_MATRIX.md``.
"""

from __future__ import annotations

from flask import Blueprint, request

from traceability.auth import current_operator_id
from traceability.ble_collector import (
    BLUETOOTH_OPERATION_LOCK,
    discover_v2_devices,
    is_bluetooth_runtime_available,
    read_v2_identity,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import product_model_dict

bluetooth_bp = Blueprint("bluetooth", __name__)

# The product models that identify themselves over Bluetooth, limited to those
# the current operator is allowed to see. ``operator_id`` is bound twice because
# the statement uses it twice; NULL (an admin, who is not scoped) matches
# everything.
_BLUETOOTH_MODELS_SQL = """
    SELECT pm.*, pf.product_code AS product_family_code,
           pf.name AS product_family_name, 0 AS finished_product_count,
           0 AS trace_plan_count
    FROM product_models pm
    JOIN product_families pf ON pf.id = pm.product_family_id
    WHERE pm.active = 1 AND pf.active = 1
      AND pm.identity_source = 'BLUETOOTH'
      AND (? IS NULL OR EXISTS(
            SELECT 1 FROM user_product_model_permissions permission
            WHERE permission.user_id = ? AND permission.product_model_id = pm.id
      ))
"""


@bluetooth_bp.get("/api/bluetooth/status")
def bluetooth_status():
    operator_id = current_operator_id()
    rows = get_db().execute(
        _BLUETOOTH_MODELS_SQL + " ORDER BY pm.model_code",
        (operator_id, operator_id),
    ).fetchall()
    models = [product_model_dict(row) for row in rows]
    # The form pre-fills from the first permitted model; the operator can still
    # pick another one.
    first = models[0] if models else {}
    return success(
        {
            "available": is_bluetooth_runtime_available(),
            "models": models,
            "namePrefix": first.get("bluetoothNamePrefix", ""),
            "serviceUuid": first.get("bluetoothServiceUuid", ""),
            "notifyUuid": first.get("bluetoothNotifyUuid", ""),
        }
    )


@bluetooth_bp.post("/api/bluetooth/discover")
def bluetooth_discover():
    operator_id = current_operator_id()
    prefixes = [
        row["bluetooth_name_prefix"]
        for row in get_db()
        .execute(
            """
            SELECT pm.bluetooth_name_prefix
            FROM product_models pm JOIN product_families pf ON pf.id = pm.product_family_id
            WHERE pm.active = 1 AND pf.active = 1
              AND pm.identity_source = 'BLUETOOTH'
              AND pm.bluetooth_name_prefix <> ''
              AND (? IS NULL OR EXISTS(
                    SELECT 1 FROM user_product_model_permissions permission
                    WHERE permission.user_id = ? AND permission.product_model_id = pm.id
              ))
            """,
            (operator_id, operator_id),
        )
        .fetchall()
    ]
    if not BLUETOOTH_OPERATION_LOCK.acquire(blocking=False):
        raise ApiError("另一个蓝牙采集操作正在进行，请稍后重试", 409)
    try:
        return success(discover_v2_devices(name_prefixes=prefixes))
    finally:
        BLUETOOTH_OPERATION_LOCK.release()


@bluetooth_bp.post("/api/bluetooth/read-sn")
def bluetooth_read_sn():
    payload = request.get_json(silent=True) or {}
    database = get_db()
    operator_id = current_operator_id()
    # Longest prefix first: two models may share a prefix (TW-04 vs TW-0401) and
    # the more specific one has to win.
    models = database.execute(
        _BLUETOOTH_MODELS_SQL + " ORDER BY length(pm.bluetooth_name_prefix) DESC",
        (operator_id, operator_id),
    ).fetchall()

    selected_model = None
    if payload.get("productModelId") not in {None, ""}:
        try:
            requested_model_id = int(payload.get("productModelId"))
        except (TypeError, ValueError):
            raise ApiError("产品型号无效") from None
        selected_model = next((row for row in models if row["id"] == requested_model_id), None)
    else:
        # No explicit model: match the advertised device name against the
        # permitted prefixes.
        device_name = str(payload.get("name") or "")
        selected_model = next(
            (
                row
                for row in models
                if row["bluetooth_name_prefix"]
                and device_name.startswith(row["bluetooth_name_prefix"])
            ),
            None,
        )
    if not selected_model:
        raise ApiError("该蓝牙设备不属于已启用的产品型号", 409)

    if not BLUETOOTH_OPERATION_LOCK.acquire(blocking=False):
        raise ApiError("另一个蓝牙采集操作正在进行，请稍后重试", 409)
    try:
        identity = read_v2_identity(
            payload.get("address"),
            payload.get("name"),
            notify_uuid=selected_model["bluetooth_notify_uuid"],
        )
    finally:
        BLUETOOTH_OPERATION_LOCK.release()

    # The device reports its own model; a mismatch means the operator picked the
    # wrong row (or the firmware was flashed wrong), and registering it under the
    # selected model would put a wrong part in the traceability chain.
    reported_model = str(identity.get("model") or "").strip()
    if reported_model and reported_model.upper() != selected_model["model_code"].upper():
        raise ApiError(
            f"设备上报型号 {reported_model}，与所选型号 {selected_model['model_code']} 不一致",
            409,
        )
    existing = database.execute(
        "SELECT id FROM machines WHERE sn = ?", (identity["sn"],)
    ).fetchone()
    return success(
        {
            **identity,
            "model": selected_model["model_code"],
            "productModel": product_model_dict(selected_model),
            "alreadyRegistered": bool(existing),
            "machineId": existing["id"] if existing else None,
        }
    )
