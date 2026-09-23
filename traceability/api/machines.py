"""Finished machines (成品) — the units registered against a product model.

A machine is one physical finished product. Its SN is the identity everything
downstream hangs off: the trace plan snapshot, the scan sessions, the trace
records. Commissioning one is therefore audited, and the SN prefix is checked
against the model's ``serial_prefix`` so a unit cannot be filed under the wrong
model — which would put a wrong serial into the traceability chain.

The QR route lives here too even though it sits far from the other two in the
original file; it is the same resource.
"""

from __future__ import annotations

from flask import Blueprint, Response, request

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_operator_id,
    require_admin,
    require_capability,
    require_product_model_access,
)
from traceability.capabilities import Capability
from traceability.codes import (
    machine_identification_code,
    make_qr_svg,
    normalize_sn,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import machine_dict
from traceability.validators import clean_text, now_iso

machines_bp = Blueprint("machines", __name__)

# The list and the create response return the same joined shape and differ only
# in how ``traced`` and ``reserved_station`` are derived — a brand new machine has
# neither. The join block is shared by string concatenation rather than
# ``str.format``: the statements contain ``?`` placeholders and could one day
# contain a brace, and ``format`` would fail on it at runtime.
_MACHINE_JOINS = """
    FROM machines m
    LEFT JOIN trace_plans tp ON tp.id = m.trace_plan_id
    LEFT JOIN product_models pm ON pm.id = m.product_model_id
    LEFT JOIN product_families pf ON pf.id = pm.product_family_id
"""


def _machine_select(traced: str, reserved_station: str) -> str:
    return (
        f"""
    SELECT m.*, tp.version AS trace_plan_version, tp.name AS trace_plan_name,
           pm.name AS product_model_name, pf.id AS product_family_id,
           pf.product_code AS product_family_code, pf.name AS product_family_name,
           {traced} AS traced, {reserved_station} AS reserved_station
    """
        + _MACHINE_JOINS
    )


@machines_bp.get("/api/machines")
def list_machines():
    search = clean_text(request.args.get("search"), "搜索内容", max_length=100)
    wildcard = f"%{search}%"
    operator_id = current_operator_id()
    rows = get_db().execute(
        _machine_select(
            "EXISTS(SELECT 1 FROM trace_records tr WHERE tr.machine_id = m.id)",
            "(SELECT ss.station_name FROM scan_sessions ss "
            "WHERE ss.machine_id = m.id LIMIT 1)",
        )
        + """
        WHERE (? = '' OR m.sn LIKE ? OR m.model LIKE ? OR m.identification_code LIKE ?)
          AND (? IS NULL OR EXISTS(
                SELECT 1 FROM user_product_model_permissions permission
                WHERE permission.user_id = ? AND permission.product_model_id = m.product_model_id
          ))
        ORDER BY m.created_at DESC, m.id DESC LIMIT 500
        """,
        (search, wildcard, wildcard, wildcard, operator_id, operator_id),
    ).fetchall()
    return success([machine_dict(row) for row in rows])


@machines_bp.post("/api/machines")
def create_machine():
    require_admin()
    payload = request.get_json(silent=True) or {}
    sn = normalize_sn(payload.get("sn"))
    database = get_db()

    # The model may be given by id (the picker) or by code (a scanner or an
    # import); both resolve to the same row.
    product_model = None
    raw_model_id = payload.get("productModelId")
    if raw_model_id not in {None, ""}:
        try:
            product_model_id = int(raw_model_id)
        except (TypeError, ValueError):
            raise ApiError("请选择产品型号") from None
        product_model = database.execute(
            "SELECT * FROM product_models WHERE id = ? AND active = 1",
            (product_model_id,),
        ).fetchone()
    else:
        model_input = clean_text(payload.get("model"), "产品型号", required=True, max_length=80)
        product_model = database.execute(
            "SELECT * FROM product_models WHERE model_code = ? COLLATE NOCASE AND active = 1",
            (model_input,),
        ).fetchone()
    if not product_model:
        raise ApiError("产品型号不存在或已停用")
    product_model_id = product_model["id"]
    require_product_model_access(product_model_id)

    model = product_model["model_code"]
    serial_prefix = str(product_model["serial_prefix"] or "").strip().upper()
    if serial_prefix and not sn.startswith(serial_prefix):
        raise ApiError(f"成品 SN 必须以 {serial_prefix} 开头，不能登记到型号 {model}")
    production_date = clean_text(payload.get("productionDate"), "生产日期", max_length=8)
    if production_date and (len(production_date) != 8 or not production_date.isdigit()):
        raise ApiError("生产日期格式需为 YYYYMMDD")

    # Snapshot the currently active plan. A machine keeps the plan it was built
    # under even if the plan is later revised, so its trace records stay
    # interpretable.
    active_plan = database.execute(
        "SELECT id FROM trace_plans WHERE product_model_id = ? AND status = 'ACTIVE'",
        (product_model_id,),
    ).fetchone()
    timestamp = now_iso()
    database.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.execute(
            """
            INSERT INTO machines(
                sn, model, product_model_id, production_date,
                trace_plan_id, identification_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sn,
                model,
                product_model_id,
                production_date,
                active_plan["id"] if active_plan else None,
                machine_identification_code(sn),
                timestamp,
            ),
        )
        record_audit_event(
            database,
            "MACHINE_COMMISSIONED",
            "MACHINE",
            sn,
            payload={
                "model": model,
                "productionDate": production_date,
                "tracePlanId": active_plan["id"] if active_plan else None,
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute(
        _machine_select("0", "NULL") + "WHERE m.id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return success(machine_dict(row), 201)


@machines_bp.get("/api/machines/<int:machine_id>/qr")
def machine_qr(machine_id: int):
    require_capability(Capability.TRACE_VIEW)
    row = get_db().execute(
        "SELECT identification_code, product_model_id FROM machines WHERE id = ?",
        (machine_id,),
    ).fetchone()
    if not row:
        raise ApiError("成品不存在", 404)
    if row["product_model_id"] is None:
        raise ApiError("该成品尚未关联产品型号", 409)
    require_product_model_access(row["product_model_id"])
    return Response(make_qr_svg(row["identification_code"]), mimetype="image/svg+xml")
