"""Production batches — generating a batch and reading it back.

A production batch is one print run of QR codes for a product model. Creating one
is the heaviest write in the system: it mints the codes, deducts supplier stock
inside a ``BEGIN IMMEDIATE`` transaction, records the consumption that makes
reverse tracing possible, and is wrapped in ``run_idempotent`` so a scan gun
retrying over a flaky LAN cannot double-deduct.

Only ADMIN and WAREHOUSE may generate one; the read endpoints additionally
require TRACE_VIEW.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from flask import Blueprint, Response, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_operator_id,
    require_admin_or_warehouse,
    require_capability,
    require_product_model_access,
)
from traceability.capabilities import Capability
from traceability.codes import (
    batch_identification_code,
    make_qr_svg,
    new_batch_code,
    normalize_entity_code,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.idempotent_http import run_idempotent
from traceability.inventory import deduct_batch_inventory
from traceability.production import (
    production_batch_registration,
    production_batch_reverse_trace,
)
from traceability.responses import success
from traceability.serializers import production_batch_dict

production_batches_bp = Blueprint("production_batches", __name__)


@production_batches_bp.post("/api/production-batches")
def create_production_batch():
    return run_idempotent("production-batches.create", _impl_create_production_batch)

def _impl_create_production_batch():
    # ADMIN or WAREHOUSE may generate a production batch; the "admin only"
    # authorization has been widened (Requirements 9.1, 12.4). Unauthorized
    # roles are rejected before any batch is created or stock is deducted
    # (Requirement 9.3); unauthenticated requests are stopped by
    # ``before_request`` (Requirement 9.5).
    require_admin_or_warehouse()
    payload = request.get_json(silent=True) or {}

    # Missing product model or prefix is rejected with a descriptive error
    # naming the missing field, before creating anything (Requirement 1.7).
    product_model_value = payload.get("productModelId")
    if product_model_value is None or (
        isinstance(product_model_value, str) and not product_model_value.strip()
    ):
        raise ApiError("请选择产品型号")
    try:
        product_model_id = int(product_model_value)
    except (TypeError, ValueError) as error:
        raise ApiError("产品型号无效") from error

    prefix = normalize_entity_code(payload.get("prefix"), "前缀")

    # Planned quantity must be an integer in 1..999999 (inclusive). Empty,
    # non-integer, out-of-range values are rejected without creating a batch,
    # generating a QR or deducting any stock (Requirements 1.3, 1.4).
    raw_quantity = payload.get("quantity")
    if raw_quantity is None or (
        isinstance(raw_quantity, str) and not raw_quantity.strip()
    ):
        raise ApiError("请输入计划台数，取值范围为 1-999999 的整数")
    if isinstance(raw_quantity, bool) or (
        isinstance(raw_quantity, float) and not raw_quantity.is_integer()
    ):
        raise ApiError("计划台数必须是 1-999999 之间的整数")
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError) as error:
        raise ApiError("计划台数必须是 1-999999 之间的整数") from error
    if not 1 <= quantity <= 999999:
        raise ApiError("计划台数必须是 1-999999 之间的整数")

    database = get_db()
    product = database.execute(
        "SELECT * FROM product_models WHERE id = ? AND active = 1",
        (product_model_id,),
    ).fetchone()
    if not product:
        raise ApiError("产品不存在或已停用", 404)

    # A warehouse operator must be authorized for the product model; admins
    # bypass the scope check (Requirement 9.2, design 6.1). This runs after
    # the product exists so callers get a stable authorization error.
    require_product_model_access(product_model_id)

    plan = database.execute(
        """
        SELECT * FROM trace_plans
        WHERE product_model_id = ? AND status = 'ACTIVE'
        ORDER BY activated_at DESC, id DESC LIMIT 1
        """,
        (product_model_id,),
    ).fetchone()
    if not plan:
        raise ApiError("该产品尚未配置生产计划，无法生成批次", 409)

    timestamp = current_app.config["NOW_PROVIDER"]()
    try:
        date_code = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        ).strftime("%Y%m%d")
    except ValueError:
        date_code = datetime.now().astimezone().strftime("%Y%m%d")
    actor_user_id = current_actor_id()
    generated_by = current_actor_name("系统管理员")
    batch_code = new_batch_code(date_code, prefix)

    database.execute("BEGIN IMMEDIATE")
    try:
        # Insert the batch first so the ISSUE movements can reference it. The
        # UNIQUE COLLATE NOCASE constraint on batch_code rejects a duplicate
        # code value (Requirement 1.9). Because this endpoint always mints a
        # fresh code for a new batch and never re-codes an existing batch,
        # an existing-batch re-code is structurally impossible here
        # (Requirement 11.4); re-printing reuses the code via GET .../qr.
        cursor = database.execute(
            """
            INSERT INTO production_batches(
                batch_code, product_model_id, trace_plan_id, prefix,
                planned_quantity, generated_by, generated_by_user_id,
                generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_code,
                product_model_id,
                plan["id"],
                prefix,
                quantity,
                generated_by,
                actor_user_id,
                timestamp,
            ),
        )
        production_batch_id = cursor.lastrowid

        # Deduct supplier inventory by N × per-unit usage and write ISSUE
        # movements within this same transaction (Requirements 4.1, 4.3). A
        # shortfall raises and rolls the whole thing back (Requirement 4.2).
        consumption = deduct_batch_inventory(
            database,
            trace_plan_id=plan["id"],
            product_model_id=product_model_id,
            quantity=quantity,
            production_batch_id=production_batch_id,
            batch_code=batch_code,
            actor_user_id=actor_user_id,
            timestamp=timestamp,
        )

        # Persist the batch-level supplier-batch consumption records so that
        # per-part consumed amounts sum to N × per-unit usage (Requirements
        # 5.1, 5.2).
        for item in consumption:
            database.execute(
                """
                INSERT INTO production_batch_supplier_consumption(
                    production_batch_id, supplier_inventory_batch_id,
                    part_type_id, quantity_consumed
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    production_batch_id,
                    item["supplier_inventory_batch_id"],
                    item["part_type_id"],
                    item["quantity_consumed"],
                ),
            )

        record_audit_event(
            database,
            "PRODUCTION_BATCH_GENERATED",
            "PRODUCTION_BATCH",
            batch_code,
            related_object_code=product["model_code"],
            payload={
                "productModelId": product_model_id,
                "plannedQuantity": quantity,
                "prefix": prefix,
                "tracePlanId": plan["id"],
            },
            occurred_at=timestamp,
        )
        database.commit()
    except sqlite3.IntegrityError as error:
        database.rollback()
        # A duplicate batch code value: reject without creating the batch or
        # its QR (Requirement 1.9).
        if "batch_code" in str(error).lower():
            raise ApiError("批次码值重复，请重试", 409) from error
        raise
    except Exception:
        database.rollback()
        raise

    row = database.execute(
        "SELECT * FROM production_batches WHERE id = ?",
        (production_batch_id,),
    ).fetchone()
    return success(production_batch_dict(database, row), 201)

@production_batches_bp.get("/api/production-batches")
def list_production_batches():
    # Admins see every production batch; a warehouse operator only sees
    # batches whose product model is authorized to them (Requirement 1.6,
    # design 6.2). ``current_operator_id`` returns ``None`` for admins and
    # the acting user id for scoped (warehouse) users.
    database = get_db()
    operator_id = current_operator_id()
    # Join the product model columns so serialization needs no per-row
    # product lookup (see production_batch_dict).
    if operator_id is None:
        rows = database.execute(
            """
            SELECT pb.*, pm.model_code AS model_code, pm.name AS product_name
            FROM production_batches pb
            LEFT JOIN product_models pm ON pm.id = pb.product_model_id
            ORDER BY pb.generated_at DESC, pb.id DESC
            """
        ).fetchall()
    else:
        rows = database.execute(
            """
            SELECT pb.*, pm.model_code AS model_code, pm.name AS product_name
            FROM production_batches pb
            JOIN user_product_model_permissions permission
                ON permission.product_model_id = pb.product_model_id
            LEFT JOIN product_models pm ON pm.id = pb.product_model_id
            WHERE permission.user_id = ?
            ORDER BY pb.generated_at DESC, pb.id DESC
            """,
            (operator_id,),
        ).fetchall()
    # Resolve every batch's registration status in one query instead of one
    # lookup per batch (avoids an N+1 over batch_trace_records).
    registrations: dict[int, sqlite3.Row] = {}
    batch_ids = [row["id"] for row in rows]
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        registration_rows = database.execute(
            f"""
            SELECT production_batch_id, registered_quantity, quality_status
            FROM batch_trace_records
            WHERE production_batch_id IN ({placeholders})
            """,
            batch_ids,
        ).fetchall()
        registrations = {row["production_batch_id"]: row for row in registration_rows}
    payload = []
    for row in rows:
        item = production_batch_dict(database, row)
        registration = registrations.get(row["id"])
        item["registered"] = registration is not None
        item["registeredQuantity"] = (
            registration["registered_quantity"] if registration else None
        )
        item["qualityStatus"] = registration["quality_status"] if registration else None
        payload.append(item)
    return success(payload)

@production_batches_bp.get("/api/production-batches/<int:batch_id>")
def get_production_batch(batch_id: int):
    require_capability(Capability.TRACE_VIEW)
    # Batch detail carries the base fields plus registration status and the
    # batch-level reverse-trace list (design 6.2). A missing batch returns a
    # descriptive 404 (Requirement 1.8). A warehouse operator must be
    # authorized for the product model; admins bypass the scope check.
    database = get_db()
    row = database.execute(
        "SELECT * FROM production_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if not row:
        raise ApiError("生产批次不存在", 404)
    require_product_model_access(row["product_model_id"])
    detail = production_batch_dict(database, row)
    detail["registration"] = production_batch_registration(database, batch_id)
    detail["reverseTrace"] = production_batch_reverse_trace(database, batch_id)
    return success(detail)

@production_batches_bp.get("/api/production-batches/<int:batch_id>/qr")
def production_batch_qr(batch_id: int):
    require_capability(Capability.TRACE_VIEW)
    # Reuse ``make_qr_svg`` to render the batch QR. The QR encodes the
    # existing ``batch_code`` (``PTS:B:{batch_code}``) so re-printing reuses
    # the same code value rather than minting a new one (Requirements 3.1,
    # 11.3). A missing batch returns 404 (Requirement 1.8).
    row = get_db().execute(
        "SELECT batch_code, product_model_id FROM production_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if not row:
        raise ApiError("生产批次不存在", 404)
    require_product_model_access(row["product_model_id"])
    return Response(
        make_qr_svg(batch_identification_code(row["batch_code"])),
        mimetype="image/svg+xml",
    )
