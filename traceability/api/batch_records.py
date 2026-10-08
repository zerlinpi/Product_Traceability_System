"""Batch registration and batch quality — HTTP layer.

The batch-level traceability flow once a production batch exists:

* ``POST /api/batch-entry/scan`` registers a whole batch from one scan of its QR
  (one ``batch_trace_records`` row, never one per unit). It is idempotent.
* ``POST /api/batch-trace-records/<id>/pass`` and ``/hold`` are the quality
  decision on a registration (ASSEMBLED to PASSED or HOLD).
* ``GET /api/batch-trace-records`` lists registrations by batch code or time.
* ``POST /api/batch-trace/query`` is the read-only trace of a scanned batch QR,
  down to the supplier batches it consumed.

``_impl_batch_entry_scan`` and ``transition_batch_quality`` hold the guards of
the idempotent entry route and of the pass route. They stay module-level
functions of this file on purpose: ``tools/extract_routes.py`` resolves a
route's guards from its handler and the same-file functions it calls, so a guard
inside a domain module would vanish from the permission matrix.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.pagination import parse_list_window
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_operator_id,
    require_admin,
    require_capability,
    require_product_model_access,
    require_warehouse,
)
from traceability.capabilities import Capability
from traceability.codes import batch_identification_code, parse_batch_payload
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.idempotent_http import run_idempotent
from traceability.production import (
    production_batch_registration,
    production_batch_reverse_trace,
)
from traceability.responses import success
from traceability.serializers import batch_trace_record_dict
from traceability.validators import clean_text

batch_records_bp = Blueprint("batch_records", __name__)


@batch_records_bp.post("/api/batch-entry/scan")
def batch_entry_scan():
    return run_idempotent("batch-entry.scan", _impl_batch_entry_scan)


def _impl_batch_entry_scan():
    # The field / warehouse operator scans a batch QR to register the whole
    # batch in one shot (Requirement 2.1). The former "operator" field-entry
    # ability is owned by WAREHOUSE (ADMIN is allowed for support); other
    # roles are rejected before parsing or writing the registration.
    require_warehouse()
    payload = request.get_json(silent=True) or {}

    # Resolve the scanned code -> batch_code. An empty / unparseable code is
    # rejected with a descriptive error and creates nothing (Requirement
    # 2.3).
    try:
        batch_code = parse_batch_payload(payload.get("code"))
    except ValueError as error:
        raise ApiError("批次二维码无效或不存在", 404) from error

    database = get_db()
    batch = database.execute(
        "SELECT * FROM production_batches WHERE batch_code = ? COLLATE NOCASE",
        (batch_code,),
    ).fetchone()
    if not batch:
        # A nonexistent batch: reject and create no record (Requirement 2.3).
        raise ApiError("批次二维码无效或不存在", 404)

    # A warehouse operator must be authorized for the batch's product model;
    # an unauthorized operator is rejected without creating any record
    # (Requirements 2.3, 9.4). Admins bypass the scope check (Requirement
    # 9.2).
    require_product_model_access(batch["product_model_id"])

    planned_quantity = int(batch["planned_quantity"])

    # Optional registered-quantity correction: when omitted it defaults to
    # the batch's planned quantity (Requirement 2.1); when provided it must
    # be an integer in 1..planned_quantity, otherwise the correction is
    # rejected and no record is created or updated (Requirements 2.5, 2.6).
    raw_quantity = payload.get("quantity")
    if raw_quantity is None or (
        isinstance(raw_quantity, str) and not raw_quantity.strip()
    ):
        registered_quantity = planned_quantity
    else:
        if isinstance(raw_quantity, bool) or (
            isinstance(raw_quantity, float) and not raw_quantity.is_integer()
        ):
            raise ApiError(f"登记台数必须是 1-{planned_quantity} 之间的整数")
        try:
            registered_quantity = int(raw_quantity)
        except (TypeError, ValueError) as error:
            raise ApiError(
                f"登记台数必须是 1-{planned_quantity} 之间的整数"
            ) from error
        if not 1 <= registered_quantity <= planned_quantity:
            raise ApiError(f"登记台数必须是 1-{planned_quantity} 之间的整数")

    # Optional station / operator metadata carried on the single record. The
    # operator name defaults to the acting user's display name.
    station_id = clean_text(payload.get("stationId"), "工位标识", max_length=80)
    station_name = clean_text(payload.get("stationName"), "工位名称", max_length=80)
    operator_name = clean_text(
        payload.get("operatorName"), "操作人", max_length=60
    ) or current_actor_name("")

    # Reject a duplicate registration up-front with a friendly message; the
    # UNIQUE constraint on production_batch_id guards the concurrent race
    # (Requirement 2.4).
    if database.execute(
        "SELECT 1 FROM batch_trace_records WHERE production_batch_id = ?",
        (batch["id"],),
    ).fetchone():
        raise ApiError("该批次已登记，无法重复登记", 409)

    timestamp = current_app.config["NOW_PROVIDER"]()
    actor_user_id = current_actor_id()

    database.execute("BEGIN IMMEDIATE")
    try:
        # Insert a single batch_trace_records row and no per-unit records
        # (Requirement 2.2); quality status initializes to ASSEMBLED
        # (Requirement 6.1).
        cursor = database.execute(
            """
            INSERT INTO batch_trace_records(
                production_batch_id, registered_quantity, quality_status,
                station_id, station_name, operator_name,
                completed_by_user_id, registered_at
            ) VALUES (?, ?, 'ASSEMBLED', ?, ?, ?, ?, ?)
            """,
            (
                batch["id"],
                registered_quantity,
                station_id,
                station_name,
                operator_name,
                actor_user_id,
                timestamp,
            ),
        )
        record_id = cursor.lastrowid
        record_audit_event(
            database,
            "BATCH_REGISTERED",
            "BATCH_TRACE_RECORD",
            batch["batch_code"],
            related_object_code=str(batch["product_model_id"]),
            station_id=station_id,
            station_name=station_name,
            operator_name=operator_name,
            payload={
                "productModelId": batch["product_model_id"],
                "registeredQuantity": registered_quantity,
                "plannedQuantity": planned_quantity,
            },
            occurred_at=timestamp,
        )
        database.commit()
    except sqlite3.IntegrityError as error:
        database.rollback()
        # A concurrent duplicate registration hits the UNIQUE constraint on
        # production_batch_id: keep the existing record unchanged
        # (Requirement 2.4).
        raise ApiError("该批次已登记，无法重复登记", 409) from error
    except Exception:
        database.rollback()
        raise

    product = database.execute(
        "SELECT model_code, name FROM product_models WHERE id = ?",
        (batch["product_model_id"],),
    ).fetchone()
    return success(
        {
            "id": record_id,
            "productionBatchId": batch["id"],
            "batchCode": batch["batch_code"],
            "productModelId": batch["product_model_id"],
            "productModelCode": product["model_code"] if product else None,
            "productModelName": product["name"] if product else None,
            "productName": product["name"] if product else None,
            "prefix": batch["prefix"],
            "plannedQuantity": planned_quantity,
            "registeredQuantity": registered_quantity,
            "qualityStatus": "ASSEMBLED",
            "stationId": station_id,
            "stationName": station_name,
            "operatorName": operator_name,
            "completedByUserId": actor_user_id,
            "registeredAt": timestamp,
        },
        201,
    )


def transition_batch_quality(
    record_id: int, next_status: str, reason: str
) -> dict[str, Any]:
    # Shared ASSEMBLED -> {PASSED, HOLD} transition for the batch-level
    # quality closed loop (Requirement 6). Only ADMIN may release or hold a
    # batch (design BatchQualityService); unauthenticated requests are
    # stopped by ``before_request``.
    require_admin()
    database = get_db()
    row = database.execute(
        """
        SELECT r.id, r.quality_status, b.batch_code, b.product_model_id
        FROM batch_trace_records r
        JOIN production_batches b ON b.id = r.production_batch_id
        WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if not row:
        raise ApiError("批次登记记录不存在", 404)

    current_status = row["quality_status"]
    # Only a record currently in ASSEMBLED may transition; any other status
    # is rejected and left unchanged (Requirement 6.5).
    if current_status != "ASSEMBLED":
        raise ApiError("仅待检状态的批次登记记录可以放行或暂扣", 409)

    timestamp = current_app.config["NOW_PROVIDER"]()
    actor_user_id = current_actor_id()

    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            """
            UPDATE batch_trace_records
            SET quality_status = ?, status_reason = ?, status_updated_at = ?,
                status_updated_by_user_id = ?
            WHERE id = ?
            """,
            (next_status, reason, timestamp, actor_user_id, record_id),
        )
        # Write an audit event in the same transaction recording the actor,
        # the before / after status and the change time; a HOLD carries the
        # reason (Requirement 6.6).
        audit_payload: dict[str, Any] = {
            "fromStatus": current_status,
            "toStatus": next_status,
        }
        if next_status == "HOLD":
            audit_payload["reason"] = reason
        record_audit_event(
            database,
            "BATCH_TRACE_RECORD_STATUS_CHANGED",
            "BATCH_TRACE_RECORD",
            row["batch_code"],
            related_object_code=str(row["product_model_id"]),
            operator_name=current_actor_name("系统管理员"),
            reason=reason,
            payload=audit_payload,
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise

    return {
        "id": record_id,
        "batchCode": row["batch_code"],
        "productModelId": row["product_model_id"],
        "qualityStatus": next_status,
        "statusReason": reason,
        "statusUpdatedAt": timestamp,
        "statusUpdatedByUserId": actor_user_id,
    }


@batch_records_bp.post("/api/batch-trace-records/<int:record_id>/pass")
def pass_batch_trace_record(record_id: int):
    # ADMIN releases a batch registration record: ASSEMBLED -> PASSED
    # (Requirement 6.2). A non-ASSEMBLED record is rejected unchanged
    # (Requirement 6.5).
    return success(transition_batch_quality(record_id, "PASSED", ""))


@batch_records_bp.post("/api/batch-trace-records/<int:record_id>/hold")
def hold_batch_trace_record(record_id: int):
    # ADMIN holds a batch registration record: ASSEMBLED -> HOLD with a
    # reason of 1..500 characters (Requirement 6.3). An empty / whitespace
    # only / over-500-character reason is rejected and the quality status is
    # left unchanged (Requirement 6.4).
    require_admin()
    payload = request.get_json(silent=True) or {}
    raw_reason = payload.get("reason")
    reason = str(raw_reason or "").strip()
    if not 1 <= len(reason) <= 500:
        raise ApiError("暂扣原因必须为 1-500 个字符")
    return success(transition_batch_quality(record_id, "HOLD", reason))


@batch_records_bp.get("/api/batch-trace-records")
def list_batch_trace_records():
    # Query batch registration records by generation batch (batchCode) or by
    # a generation-time range (Requirements 3.3, 3.4). "生成时间" is the
    # production batch's ``generated_at``. Both range boundaries are
    # inclusive; results are ordered from most-recently generated to oldest.
    # No match yields an empty list rather than an error (Requirement 3.5);
    # a ``from`` later than ``to`` is rejected with a descriptive error
    # (Requirement 3.6).
    database = get_db()

    batch_code = clean_text(
        request.args.get("batchCode"), "批次码值", max_length=64
    )
    range_from = clean_text(request.args.get("from"), "起始时间", max_length=64)
    range_to = clean_text(request.args.get("to"), "结束时间", max_length=64)

    # The range is only well-defined when both boundaries are supplied; a
    # start later than the end is an invalid range (Requirement 3.6).
    if range_from and range_to and range_from > range_to:
        raise ApiError("起始时间不能晚于结束时间")

    clauses: list[str] = []
    parameters: list[Any] = []
    if batch_code:
        clauses.append("b.batch_code = ? COLLATE NOCASE")
        parameters.append(batch_code)
    if range_from:
        clauses.append("b.generated_at >= ?")
        parameters.append(range_from)
    if range_to:
        clauses.append("b.generated_at <= ?")
        parameters.append(range_to)

    # Admins see every record; a warehouse operator only sees records whose
    # product model is authorized to them (Requirement 9.2 / design 6.2).
    # ``current_operator_id`` returns ``None`` for admins.
    operator_id = current_operator_id()
    if operator_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM user_product_model_permissions permission "
            "WHERE permission.user_id = ? "
            "AND permission.product_model_id = b.product_model_id)"
        )
        parameters.append(operator_id)

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # Bounded like the other list endpoints. This table gains a row per
    # registration, so an unbounded response reached 10 MB at 30,000 rows and the
    # browser renders them without virtualisation.
    window = parse_list_window()
    total = database.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM batch_trace_records r
        JOIN production_batches b ON b.id = r.production_batch_id
        {where_clause}
        """,
        parameters,
    ).fetchone()["n"]

    rows = database.execute(
        f"""
        SELECT r.id AS id,
               r.production_batch_id AS production_batch_id,
               r.registered_quantity AS registered_quantity,
               r.quality_status AS quality_status,
               r.operator_name AS operator_name,
               r.registered_at AS registered_at,
               b.batch_code AS batch_code,
               b.prefix AS prefix,
               b.generated_at AS generated_at,
               b.product_model_id AS product_model_id,
               pm.model_code AS product_model_code,
               pm.name AS product_name
        FROM batch_trace_records r
        JOIN production_batches b ON b.id = r.production_batch_id
        LEFT JOIN product_models pm ON pm.id = b.product_model_id
        {where_clause}
        ORDER BY b.generated_at DESC, b.id DESC
        LIMIT ? OFFSET ?
        """,
        [*parameters, window.limit, window.offset],
    ).fetchall()
    return window.apply(success([batch_trace_record_dict(row) for row in rows]), total)


@batch_records_bp.post("/api/batch-trace/query")
def batch_trace_query():
    require_capability(Capability.TRACE_VIEW)
    # An authenticated user scans a batch QR to perform a READ-ONLY
    # traceability lookup (Requirement 10.1). Authentication is enforced by
    # ``before_request`` (unauthenticated -> 401, Requirement 10.8); any
    # authenticated user may query. This endpoint performs pure SELECTs and
    # never creates, modifies or deletes any production batch, batch
    # registration record or supplier inventory data (Requirement 10.3).
    payload = request.get_json(silent=True) or {}

    # Resolve the scanned code -> batch_code. An empty / unparseable code is
    # rejected with a descriptive error and returns no traceability info
    # (Requirement 10.7).
    try:
        batch_code = parse_batch_payload(payload.get("code"))
    except ValueError as error:
        raise ApiError("批次二维码无效或不存在", 404) from error

    database = get_db()
    batch = database.execute(
        "SELECT * FROM production_batches WHERE batch_code = ? COLLATE NOCASE",
        (batch_code,),
    ).fetchone()
    if not batch:
        # A nonexistent batch: reject and return no traceability info
        # (Requirement 10.7).
        raise ApiError("批次二维码无效或不存在", 404)

    # An admin may query any existing batch (Requirement 10.4); a warehouse
    # operator may only query batches whose product model is authorized to
    # them, otherwise the query is rejected with an insufficient-permission
    # error (Requirements 10.5, 10.6). ``require_product_model_access``
    # returns without restriction for admins.
    require_product_model_access(batch["product_model_id"])

    product = database.execute(
        "SELECT model_code, name FROM product_models WHERE id = ?",
        (batch["product_model_id"],),
    ).fetchone()

    # The registered quantity and quality status come from the batch's
    # registration record when it exists; an unregistered batch reports a
    # registered quantity of 0 and no quality status (Requirement 10.1).
    registration = production_batch_registration(database, batch["id"])
    if registration["registered"]:
        registered_quantity = registration["registeredQuantity"]
        quality_status = registration["qualityStatus"]
    else:
        registered_quantity = 0
        quality_status = None

    return success(
        {
            "id": batch["id"],
            "batchCode": batch["batch_code"],
            "identificationCode": batch_identification_code(batch["batch_code"]),
            "productModelId": batch["product_model_id"],
            "productModelCode": product["model_code"] if product else None,
            "productName": product["name"] if product else None,
            "plannedQuantity": batch["planned_quantity"],
            "registeredQuantity": registered_quantity,
            "generatedAt": batch["generated_at"],
            "prefix": batch["prefix"],
            "qualityStatus": quality_status,
            "registered": registration["registered"],
            # Batch-level reverse trace: the supplier inventory batches this
            # production batch consumed (Requirement 10.2).
            "reverseTrace": production_batch_reverse_trace(database, batch["id"]),
        }
    )
