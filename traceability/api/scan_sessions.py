"""Legacy per-unit scan station (逐台扫码) — HTTP layer.

One station works through one finished product at a time: scan its main code,
then each part code in BOM order (or a fixed count when the product has no BOM).
The last part completes a ``trace_records`` row and clears the station. The
in-progress session is kept per station in ``scan_sessions``;
``/api/scan/undo`` drops the last scanned part and ``/api/scan/reset`` abandons
the unit.

Treadmill products are refused at every step (see
``traceability/legacy_scan.py``): they moved to batch-level traceability, where
one scan registers a whole batch (``batch_records.py``).

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, request

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    require_capability,
    require_product_model_access,
    require_supplier_access,
)
from traceability.capabilities import Capability
from traceability.codes import new_trace_number, normalize_station_id
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.legacy_scan import (
    DEFAULT_GENERIC_PART_COUNT,
    LEGACY_ENTRY_DISABLED_MESSAGE,
    SESSION_TIMEOUT_HOURS,
    ensure_station_access,
    is_treadmill_product_model,
    session_state,
)
from traceability.quality import require_quality_release
from traceability.responses import success
from traceability.trace_plans import get_plan_summary, trace_plan_slots
from traceability.trace_records import fetch_records
from traceability.validators import clean_text, now_iso

scan_sessions_bp = Blueprint("scan_sessions", __name__)


@scan_sessions_bp.get("/api/scan/session")
def get_scan_session():
    station_id = normalize_station_id(request.args.get("stationId"))
    database = get_db()
    ensure_station_access(database, station_id)
    return success(session_state(database, station_id))


@scan_sessions_bp.post("/api/scan/reset")
def reset_scan_session():
    payload = request.get_json(silent=True) or {}
    station_id = normalize_station_id(payload.get("stationId"))
    database = get_db()
    ensure_station_access(database, station_id)
    session = database.execute(
        """
        SELECT ss.*, m.sn, m.product_model_id FROM scan_sessions ss
        LEFT JOIN machines m ON m.id = ss.machine_id
        WHERE ss.station_id = ?
        """,
        (station_id,),
    ).fetchone()
    # Requirement 7.3: reset is part of the retired treadmill per-unit
    # assembly entry. Reject when the station holds an in-progress treadmill
    # session; non-treadmill sessions still reset normally (Requirement 7.5).
    if (
        session
        and session["machine_id"]
        and is_treadmill_product_model(database, session["product_model_id"])
    ):
        raise ApiError(LEGACY_ENTRY_DISABLED_MESSAGE, 409)
    database.execute("BEGIN IMMEDIATE")
    try:
        if session and session["machine_id"]:
            scanned_count = database.execute(
                "SELECT COUNT(*) AS n FROM scan_session_items WHERE station_id = ?",
                (station_id,),
            ).fetchone()["n"]
            record_audit_event(
                database,
                "ASSEMBLY_CANCELLED",
                "MACHINE",
                session["sn"],
                station_id=station_id,
                station_name=session["station_name"],
                operator_name=session["operator_name"],
                reason=clean_text(payload.get("reason"), "清空原因", max_length=160),
                payload={"scannedPartCount": scanned_count},
            )
        database.execute("DELETE FROM scan_sessions WHERE station_id = ?", (station_id,))
        database.commit()
    except Exception:
        database.rollback()
        raise
    return success(session_state(database, station_id))


@scan_sessions_bp.post("/api/scan/undo")
def undo_last_scan():
    payload = request.get_json(silent=True) or {}
    station_id = normalize_station_id(payload.get("stationId"))
    reason = clean_text(payload.get("reason"), "撤销原因", required=True, max_length=160)
    database = get_db()
    database.execute("BEGIN IMMEDIATE")
    try:
        ensure_station_access(database, station_id)
        session = database.execute(
            """
            SELECT ss.*, m.sn, m.product_model_id
            FROM scan_sessions ss
            LEFT JOIN machines m ON m.id = ss.machine_id
            WHERE ss.station_id = ?
            """,
            (station_id,),
        ).fetchone()
        if not session or not session["machine_id"]:
            raise ApiError("当前没有可撤销的装配流程", 409)
        # Requirement 7.3: undo belongs to the retired treadmill per-unit
        # assembly entry. Reject for in-progress treadmill sessions before any
        # component scan is undone; non-treadmill sessions are unaffected
        # (Requirement 7.5). Raised inside the transaction, so the outer
        # except rolls back without side effects.
        if is_treadmill_product_model(database, session["product_model_id"]):
            raise ApiError(LEGACY_ENTRY_DISABLED_MESSAGE, 409)
        item = database.execute(
            """
            SELECT ssi.position, pl.id AS part_label_id, pl.identification_code,
                   pt.part_code, pt.name AS part_name
            FROM scan_session_items ssi
            JOIN part_labels pl ON pl.id = ssi.part_label_id
            JOIN part_types pt ON pt.id = pl.part_type_id
            WHERE ssi.station_id = ?
            ORDER BY ssi.position DESC
            LIMIT 1
            """,
            (station_id,),
        ).fetchone()
        if not item:
            raise ApiError("尚未扫描部件，成品码不能通过此操作撤销；如需取消请清空本次流程", 409)
        database.execute(
            "DELETE FROM scan_session_items WHERE station_id = ? AND position = ?",
            (station_id, item["position"]),
        )
        database.execute(
            "UPDATE scan_sessions SET updated_at = ? WHERE station_id = ?",
            (now_iso(), station_id),
        )
        record_audit_event(
            database,
            "COMPONENT_SCAN_UNDONE",
            "PART_LABEL",
            item["identification_code"],
            related_object_code=session["sn"],
            station_id=station_id,
            station_name=session["station_name"],
            operator_name=session["operator_name"],
            reason=reason,
            payload={
                "position": item["position"],
                "partCode": item["part_code"],
                "partName": item["part_name"],
            },
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    return success(
        {
            "undone": {
                "position": item["position"],
                "identificationCode": item["identification_code"],
                "partCode": item["part_code"],
                "partName": item["part_name"],
            },
            "session": session_state(database, station_id),
        }
    )


@scan_sessions_bp.post("/api/scan")
def process_scan():
    require_capability(Capability.LEGACY_SCAN)
    payload = request.get_json(silent=True) or {}
    station_id = normalize_station_id(payload.get("stationId"))
    station_name = clean_text(payload.get("stationName"), "工位名称", max_length=60) or station_id[-8:]
    actor_user_id = current_actor_id()
    submitted_operator = clean_text(payload.get("operatorName"), "操作员", max_length=60)
    operator_name = (
        current_actor_name(submitted_operator)
        if actor_user_id is not None
        else submitted_operator
    )
    code = clean_text(payload.get("code"), "识别码", required=True, max_length=120).upper()
    database = get_db()
    selected_product_model_id: int | None = None
    selected_product_value = payload.get("productModelId")
    if selected_product_value not in (None, ""):
        try:
            selected_product_model_id = int(selected_product_value)
        except (TypeError, ValueError) as error:
            raise ApiError("所选产品无效，请重新选择") from error
        require_product_model_access(selected_product_model_id)

    # Requirement 7.3: the per-unit main-code + per-component assembly scan is
    # retired for treadmill products. Reject before opening the write
    # transaction so no per-unit ``trace_records`` / session state is created.
    # The treadmill product is identified from (a) the explicitly selected
    # product model, (b) the scanned finished-product ``machines`` row, or
    # (c) an in-progress session's machine (defensive: new treadmill sessions
    # can no longer be started). Non-treadmill products are unaffected
    # (Requirement 7.5); historical rows remain read-only queryable (7.4).
    if is_treadmill_product_model(database, selected_product_model_id):
        raise ApiError(LEGACY_ENTRY_DISABLED_MESSAGE, 409)
    scanned_machine = database.execute(
        "SELECT product_model_id FROM machines WHERE identification_code = ? OR sn = ?",
        (code, code),
    ).fetchone()
    if scanned_machine and is_treadmill_product_model(
        database, scanned_machine["product_model_id"]
    ):
        raise ApiError(LEGACY_ENTRY_DISABLED_MESSAGE, 409)
    active_session = database.execute(
        """
        SELECT m.product_model_id
        FROM scan_sessions ss
        JOIN machines m ON m.id = ss.machine_id
        WHERE ss.station_id = ?
        """,
        (station_id,),
    ).fetchone()
    if active_session and is_treadmill_product_model(
        database, active_session["product_model_id"]
    ):
        raise ApiError(LEGACY_ENTRY_DISABLED_MESSAGE, 409)

    completed_record_id: int | None = None
    scanned_type = ""
    database.execute("BEGIN IMMEDIATE")
    try:
        ensure_station_access(database, station_id)
        stale_before = (datetime.now().astimezone() - timedelta(hours=SESSION_TIMEOUT_HOURS)).isoformat(timespec="seconds")
        database.execute("DELETE FROM scan_sessions WHERE updated_at < ?", (stale_before,))
        timestamp = now_iso()
        database.execute(
            """
            INSERT INTO scan_sessions(station_id, station_name, operator_name, user_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(station_id) DO UPDATE SET
                station_name = excluded.station_name,
                operator_name = excluded.operator_name,
                user_id = excluded.user_id,
                updated_at = excluded.updated_at
            """,
            (station_id, station_name, operator_name, actor_user_id, timestamp),
        )
        session = database.execute(
            "SELECT * FROM scan_sessions WHERE station_id = ?", (station_id,)
        ).fetchone()

        if not session["machine_id"]:
            machine = database.execute(
                "SELECT * FROM machines WHERE identification_code = ? OR sn = ?",
                (code, code),
            ).fetchone()
            if not machine:
                if code.startswith("PTS:P:"):
                    raise ApiError("当前应扫描成品码，不能先扫描部件码")
                raise ApiError("未找到该成品，请先在成品建档中登记 SN 并生成二维码", 404)
            if machine["product_model_id"] is None:
                raise ApiError("该成品尚未关联产品型号，请由管理员先完善产品资料", 409)
            require_product_model_access(machine["product_model_id"])
            if (
                selected_product_model_id is not None
                and machine["product_model_id"] != selected_product_model_id
            ):
                raise ApiError("二维码不属于当前选择的产品，请检查产品和二维码", 409)
            if database.execute(
                "SELECT 1 FROM trace_records WHERE machine_id = ?", (machine["id"],)
            ).fetchone():
                raise ApiError("扫描重复，请检查：该产品已完成录入", 409)
            reserved = database.execute(
                "SELECT station_name FROM scan_sessions WHERE station_id <> ? AND machine_id = ?",
                (station_id, machine["id"]),
            ).fetchone()
            if reserved:
                raise ApiError(f"该成品正在工位“{reserved['station_name']}”扫码", 409)
            trace_plan_id = machine["trace_plan_id"]
            if not trace_plan_id:
                active_plan = database.execute(
                    """
                    SELECT id FROM trace_plans
                    WHERE product_model_id = ? AND status = 'ACTIVE'
                    """,
                    (machine["product_model_id"],),
                ).fetchone()
                if active_plan:
                    trace_plan_id = active_plan["id"]
                    database.execute(
                        "UPDATE machines SET trace_plan_id = ? WHERE id = ?",
                        (trace_plan_id, machine["id"]),
                    )
            database.execute(
                """
                UPDATE scan_sessions
                SET machine_id = ?, trace_plan_id = ?, updated_at = ?
                WHERE station_id = ?
                """,
                (machine["id"], trace_plan_id, timestamp, station_id),
            )
            plan = get_plan_summary(database, trace_plan_id)
            record_audit_event(
                database,
                "ASSEMBLY_STARTED",
                "MACHINE",
                machine["sn"],
                station_id=station_id,
                station_name=station_name,
                operator_name=operator_name,
                payload={
                    "workflowMode": "BOM" if plan else "GENERIC",
                    "tracePlanId": trace_plan_id,
                    "tracePlanVersion": plan["version"] if plan else None,
                },
                occurred_at=timestamp,
            )
            scanned_type = "machine"
        else:
            session_machine = database.execute(
                "SELECT product_model_id FROM machines WHERE id = ?",
                (session["machine_id"],),
            ).fetchone()
            if not session_machine or session_machine["product_model_id"] is None:
                raise ApiError("当前成品尚未关联产品型号，请由管理员先完善产品资料", 409)
            require_product_model_access(session_machine["product_model_id"])
            if code.startswith("PTS:M:") or database.execute(
                "SELECT 1 FROM machines WHERE sn = ? OR identification_code = ?", (code, code)
            ).fetchone():
                raise ApiError("成品码已扫描，当前应扫描部件码")
            label = database.execute(
                """
                SELECT pl.*, pt.supplier_id
                FROM part_labels pl
                JOIN part_types pt ON pt.id = pl.part_type_id
                WHERE pl.identification_code = ?
                """,
                (code,),
            ).fetchone()
            if not label:
                raise ApiError("未找到该部件标签，请先生成部件二维码", 404)
            require_supplier_access(label["supplier_id"])
            if require_quality_release(database) and label["inspection_status"] != "PASS":
                quality_labels = {
                    "PENDING": "待检验",
                    "FAIL": "不合格",
                }
                quality_name = quality_labels.get(label["inspection_status"], label["inspection_status"] or "未设置")
                raise ApiError(
                    f"该部件当前质量状态为“{quality_name}”，不能装配；"
                    "请先在部件单码数据中完成检验并标记为合格",
                    409,
                )
            if database.execute(
                "SELECT 1 FROM trace_record_parts WHERE part_label_id = ?", (label["id"],)
            ).fetchone():
                raise ApiError("扫描重复，请检查：该部件码已经使用", 409)
            if database.execute(
                "SELECT 1 FROM scan_session_items WHERE station_id = ? AND part_label_id = ?",
                (station_id, label["id"]),
            ).fetchone():
                raise ApiError("扫描重复，请检查：当前产品已经扫过该部件", 409)
            reserved = database.execute(
                """
                SELECT ss.station_name FROM scan_session_items ssi
                JOIN scan_sessions ss ON ss.station_id = ssi.station_id
                WHERE ssi.station_id <> ? AND ssi.part_label_id = ?
                """,
                (station_id, label["id"]),
            ).fetchone()
            if reserved:
                raise ApiError(f"该部件正在工位“{reserved['station_name']}”扫码", 409)
            part_count = database.execute(
                "SELECT COUNT(*) AS n FROM scan_session_items WHERE station_id = ?", (station_id,)
            ).fetchone()["n"]
            plan_slots = trace_plan_slots(database, session["trace_plan_id"])
            required_count = len(plan_slots) if plan_slots else DEFAULT_GENERIC_PART_COUNT
            if part_count >= required_count:
                raise ApiError("当前流程部件数量已满，请清空后重试", 409)
            next_position = part_count + 1
            expected_slot = plan_slots[next_position - 1] if plan_slots else None
            bundled_set = database.execute(
                "SELECT id FROM product_code_sets WHERE machine_id = ?",
                (session["machine_id"],),
            ).fetchone()
            if bundled_set:
                bundled_part = database.execute(
                    """
                    SELECT part_label_id FROM product_code_set_parts
                    WHERE product_code_set_id = ? AND position = ?
                    """,
                    (bundled_set["id"], next_position),
                ).fetchone()
                if not bundled_part or bundled_part["part_label_id"] != label["id"]:
                    raise ApiError("部件码与当前产品不对应，请检查主码和部件码", 409)
            actual_part = database.execute(
                """
                SELECT part_code, name, category_code, category_name
                FROM part_types WHERE id = ?
                """,
                (label["part_type_id"],),
            ).fetchone()
            if expected_slot and actual_part["category_code"] != expected_slot["categoryCode"]:
                raise ApiError(
                    f"BOM 槽位“{expected_slot['slotName']}”要求 "
                    f"{expected_slot['categoryCode']} · {expected_slot['categoryName']}，"
                    f"当前扫码是 {actual_part['category_code']} · {actual_part['category_name']}",
                    409,
                )
            database.execute(
                "INSERT INTO scan_session_items(station_id, position, part_label_id) VALUES (?, ?, ?)",
                (station_id, next_position, label["id"]),
            )
            machine_identity = database.execute(
                "SELECT sn FROM machines WHERE id = ?", (session["machine_id"],)
            ).fetchone()
            record_audit_event(
                database,
                "COMPONENT_SCANNED",
                "PART_LABEL",
                label["identification_code"],
                related_object_code=machine_identity["sn"],
                station_id=station_id,
                station_name=station_name,
                operator_name=operator_name,
                payload={
                    "position": next_position,
                    "slotName": expected_slot["slotName"] if expected_slot else f"部件 {next_position}",
                    "partTypeId": label["part_type_id"],
                    "workflowMode": "BOM" if expected_slot else "GENERIC",
                },
                occurred_at=timestamp,
            )
            scanned_type = "part"

            if next_position == required_count:
                compact = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
                for trace_attempt in range(8):
                    try:
                        cursor = database.execute(
                            """
                            INSERT INTO trace_records(
                                trace_no, machine_id, trace_plan_id, status,
                                station_id, station_name, operator_name,
                                completed_by_user_id, completed_at
                            ) VALUES (?, ?, ?, 'ASSEMBLED', ?, ?, ?, ?, ?)
                            """,
                            (
                                new_trace_number(compact),
                                session["machine_id"],
                                session["trace_plan_id"],
                                station_id,
                                station_name,
                                operator_name,
                                actor_user_id,
                                timestamp,
                            ),
                        )
                        break
                    except sqlite3.IntegrityError as error:
                        if "trace_records.trace_no" not in str(error) or trace_attempt == 7:
                            raise
                completed_record_id = cursor.lastrowid
                database.execute(
                    """
                    INSERT INTO trace_record_parts(trace_record_id, position, part_label_id)
                    SELECT ?, position, part_label_id FROM scan_session_items
                    WHERE station_id = ? ORDER BY position
                    """,
                    (completed_record_id, station_id),
                )
                completed_parts = database.execute(
                    """
                    SELECT trp.position, pl.identification_code, pt.part_code
                    FROM trace_record_parts trp
                    JOIN part_labels pl ON pl.id = trp.part_label_id
                    JOIN part_types pt ON pt.id = pl.part_type_id
                    WHERE trp.trace_record_id = ? ORDER BY trp.position
                    """,
                    (completed_record_id,),
                ).fetchall()
                record_audit_event(
                    database,
                    "ASSEMBLY_COMPLETED",
                    "MACHINE",
                    machine_identity["sn"],
                    station_id=station_id,
                    station_name=station_name,
                    operator_name=operator_name,
                    payload={
                        "traceRecordId": completed_record_id,
                        "tracePlanId": session["trace_plan_id"],
                        "workflowMode": "BOM" if plan_slots else "GENERIC",
                        "components": [
                            {
                                "position": item["position"],
                                "identificationCode": item["identification_code"],
                                "partCode": item["part_code"],
                            }
                            for item in completed_parts
                        ],
                    },
                    occurred_at=timestamp,
                )
                database.execute("DELETE FROM scan_sessions WHERE station_id = ?", (station_id,))
        database.commit()
    except Exception:
        database.rollback()
        raise

    if completed_record_id:
        completed = next(
            record for record in fetch_records(database, limit=500) if record["id"] == completed_record_id
        )
        return success(
            {
                "completed": True,
                "scannedType": scanned_type,
                "record": completed,
                "session": session_state(database, station_id),
            }
        )
    return success(
        {
            "completed": False,
            "scannedType": scanned_type,
            "session": session_state(database, station_id),
        }
    )
