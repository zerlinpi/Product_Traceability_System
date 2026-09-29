"""Per-unit trace records (逐台录入记录) and genealogy — HTTP layer.

The records the legacy scan station produces: listing and exporting them,
quality handling (single and bulk status changes), correcting a record's part
codes, deleting one, and genealogy (``/api/genealogy``), which traces from a
finished product back to its parts or from a part forward to the product it
went into.

``editable_record`` carries the ownership rule for correcting and deleting a
record (a warehouse operator may only touch their own entries) together with
the product scope check. It stays a module-level function of this file on
purpose: ``tools/extract_routes.py`` resolves a route's guards from its handler
and the same-file functions it calls, so a guard inside a domain module would
vanish from the permission matrix. For the same reason
``apply_record_status_updates`` keeps its writes here, where the matrix's
``Writes`` column can see them.

The read model shared with the dashboard and the scan station is
``traceability/trace_records.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from io import BytesIO
from typing import Any

from flask import Blueprint, current_app, request, send_file

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_operator_id,
    current_user,
    require_admin,
    require_capability,
    require_product_model_access,
    require_supplier_access,
)
from traceability.capabilities import ROLE_WAREHOUSE, Capability
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.legacy_scan import DEFAULT_GENERIC_PART_COUNT
from traceability.responses import success
from traceability.serializers import audit_event_dict, machine_dict, part_label_dict
from traceability.trace_plans import get_plan_summary, trace_plan_slots
from traceability.trace_records import fetch_records
from traceability.validators import clean_text
from traceability.xlsx_export import build_traceability_xlsx

records_bp = Blueprint("records", __name__)


@records_bp.get("/api/records")
def list_records():
    require_capability(Capability.RECORD_VIEW)
    search = clean_text(request.args.get("search"), "搜索内容", max_length=100)
    record_status = clean_text(
        request.args.get("status"), "质量状态", max_length=20
    ).upper()
    allowed_statuses = {"ASSEMBLED", "PASSED", "HOLD", "VOID"}
    if record_status and record_status not in allowed_statuses:
        raise ApiError("质量状态无效")
    date_from = clean_text(request.args.get("dateFrom"), "开始日期", max_length=10)
    date_to = clean_text(request.args.get("dateTo"), "结束日期", max_length=10)
    for value, label in ((date_from, "开始日期"), (date_to, "结束日期")):
        if not value:
            continue
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as error:
            raise ApiError(f"{label}格式无效，请使用 YYYY-MM-DD") from error
    if date_from and date_to and date_from > date_to:
        raise ApiError("开始日期不能晚于结束日期")

    product_model_id: int | None = None
    product_model_value = request.args.get("productModelId")
    if product_model_value:
        try:
            product_model_id = int(product_model_value)
        except (TypeError, ValueError) as error:
            raise ApiError("产品参数无效") from error
        require_product_model_access(product_model_id)

    generation_batch_id: int | None = None
    generation_batch_value = request.args.get("generationBatchId")
    if generation_batch_value:
        try:
            generation_batch_id = int(generation_batch_value)
        except (TypeError, ValueError) as error:
            raise ApiError("生成批次参数无效") from error
        if generation_batch_id <= 0:
            raise ApiError("生成批次参数无效")
        batch = get_db().execute(
            "SELECT product_model_id FROM product_code_batches WHERE id = ?",
            (generation_batch_id,),
        ).fetchone()
        if not batch:
            raise ApiError("生成批次不存在", 404)
        require_product_model_access(batch["product_model_id"])
    return success(
        fetch_records(
            get_db(),
            search=search,
            limit=500,
            product_model_id=product_model_id,
            completed_by_user_id=current_operator_id(),
            statuses=[record_status] if record_status else None,
            generation_batch_id=generation_batch_id,
            completed_date_from=date_from,
            completed_date_to=date_to,
        )
    )


def editable_record(database: sqlite3.Connection, record_id: int) -> sqlite3.Row:
    row = database.execute(
        """
        SELECT r.*, m.sn, m.product_model_id
        FROM trace_records r JOIN machines m ON m.id = r.machine_id
        WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if not row:
        raise ApiError("录入记录不存在", 404)
    # Ownership: a warehouse operator may only modify or delete their own
    # entries. An administrator is unrestricted.
    #
    # This deliberately does NOT go through current_operator_id(). That
    # function returns None for every role — a documented decision to disable
    # product/supplier scoping — and the check that used to live here was
    # written as ``if operator_id is not None``, so it could never fire. Two
    # unrelated policies were sharing one helper, and changing the scoping
    # policy silently switched this one off. It now reads the role directly.
    actor = current_user()
    is_warehouse_operator = bool(actor) and str(actor["role"]) == ROLE_WAREHOUSE
    if is_warehouse_operator and row["completed_by_user_id"] != actor["id"]:
        raise ApiError("只能修改或删除自己的录入记录", 403)
    require_product_model_access(row["product_model_id"])
    return row


def record_status_payload(payload: dict[str, Any]) -> tuple[str, str]:
    next_status = clean_text(
        payload.get("status"), "质量状态", required=True, max_length=20
    ).upper()
    if next_status not in {"ASSEMBLED", "PASSED", "HOLD"}:
        raise ApiError("质量状态仅支持待检、合格或暂扣")
    reason = clean_text(payload.get("reason"), "处理说明", max_length=200)
    if next_status == "HOLD" and not reason:
        raise ApiError("暂扣记录必须填写原因")
    if next_status == "ASSEMBLED":
        reason = ""
    return next_status, reason


def apply_record_status_updates(
    database: sqlite3.Connection,
    records: list[sqlite3.Row],
    next_status: str,
    reason: str,
    *,
    bulk: bool = False,
) -> list[dict[str, Any]]:
    timestamp = current_app.config["NOW_PROVIDER"]()
    actor_user_id = current_actor_id()
    database.execute("BEGIN IMMEDIATE")
    try:
        for record in records:
            database.execute(
                """
                UPDATE trace_records
                SET status = ?, status_reason = ?, status_updated_at = ?,
                    status_updated_by_user_id = ?
                WHERE id = ?
                """,
                (next_status, reason, timestamp, actor_user_id, record["id"]),
            )
            record_audit_event(
                database,
                "TRACE_RECORD_STATUS_CHANGED",
                "TRACE_RECORD",
                record["trace_no"],
                related_object_code=record["sn"],
                reason=reason,
                payload={
                    "before": record["status"],
                    "after": next_status,
                    "bulk": bulk,
                    "bulkCount": len(records),
                },
                occurred_at=timestamp,
            )
        database.commit()
    except Exception:
        database.rollback()
        raise
    record_ids = [record["id"] for record in records]
    updated = fetch_records(
        database,
        limit=max(300, len(record_ids)),
        record_ids=record_ids,
    )
    updated_by_id = {item["id"]: item for item in updated}
    return [updated_by_id[record_id] for record_id in record_ids]


@records_bp.put("/api/records/status/bulk")
def update_record_status_bulk():
    require_admin()
    payload = request.get_json(silent=True) or {}
    raw_record_ids = payload.get("recordIds")
    if not isinstance(raw_record_ids, list) or not raw_record_ids:
        raise ApiError("请至少选择一条质量记录")
    if len(raw_record_ids) > 200:
        raise ApiError("单次最多批量处理 200 条质量记录")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in raw_record_ids):
        raise ApiError("质量记录参数无效")
    record_ids = list(dict.fromkeys(raw_record_ids))
    next_status, reason = record_status_payload(payload)
    placeholders = ",".join("?" for _ in record_ids)
    database = get_db()
    records = database.execute(
        f"""
        SELECT r.*, m.sn, m.product_model_id
        FROM trace_records r JOIN machines m ON m.id = r.machine_id
        WHERE r.id IN ({placeholders})
        """,
        record_ids,
    ).fetchall()
    if len(records) != len(record_ids):
        raise ApiError("部分质量记录不存在，请刷新后重试", 404)
    records_by_id = {record["id"]: record for record in records}
    ordered_records = [records_by_id[record_id] for record_id in record_ids]
    updated = apply_record_status_updates(
        database, ordered_records, next_status, reason, bulk=True
    )
    return success({"count": len(updated), "records": updated})


@records_bp.put("/api/records/<int:record_id>/status")
def update_record_status(record_id: int):
    require_admin()
    payload = request.get_json(silent=True) or {}
    next_status, reason = record_status_payload(payload)
    database = get_db()
    record = database.execute(
        """
        SELECT r.*, m.sn, m.product_model_id
        FROM trace_records r JOIN machines m ON m.id = r.machine_id
        WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if not record:
        raise ApiError("录入记录不存在", 404)
    return success(
        apply_record_status_updates(database, [record], next_status, reason)[0]
    )


@records_bp.put("/api/records/<int:record_id>")
def update_record(record_id: int):
    require_capability(Capability.RECORD_EDIT)
    payload = request.get_json(silent=True) or {}
    remarks = clean_text(payload.get("remarks"), "校对备注", max_length=200)
    part_codes_value = payload.get("partCodes")
    if not isinstance(part_codes_value, list) or not part_codes_value:
        raise ApiError("请按顺序填写全部部件二维码")
    part_codes = [
        clean_text(item, f"第 {index} 个部件码", required=True, max_length=120).upper()
        for index, item in enumerate(part_codes_value, start=1)
    ]
    if len(set(part_codes)) != len(part_codes):
        raise ApiError("扫描重复，请检查：部件码不能重复")
    database = get_db()
    record = editable_record(database, record_id)
    slots = trace_plan_slots(database, record["trace_plan_id"])
    if slots and len(part_codes) != len(slots):
        raise ApiError(f"该产品需要 {len(slots)} 个部件码")
    if not slots:
        current_count = database.execute(
            "SELECT COUNT(*) AS n FROM trace_record_parts WHERE trace_record_id = ?",
            (record_id,),
        ).fetchone()["n"]
        if len(part_codes) != current_count:
            raise ApiError(f"该记录需要 {current_count} 个部件码")

    selected_labels: list[sqlite3.Row] = []
    for position, code in enumerate(part_codes, start=1):
        label = database.execute(
            """
            SELECT pl.*, pt.category_code, pt.category_name, pt.supplier_id
            FROM part_labels pl JOIN part_types pt ON pt.id = pl.part_type_id
            WHERE pl.identification_code = ?
            """,
            (code,),
        ).fetchone()
        if not label:
            raise ApiError(f"第 {position} 个部件码不存在", 404)
        require_supplier_access(label["supplier_id"])
        used = database.execute(
            """
            SELECT trace_record_id FROM trace_record_parts
            WHERE part_label_id = ? AND trace_record_id <> ?
            """,
            (label["id"], record_id),
        ).fetchone()
        if used:
            raise ApiError(f"扫描重复，请检查：第 {position} 个部件码已用于其他产品", 409)
        if slots and label["category_code"] != slots[position - 1]["categoryCode"]:
            raise ApiError(
                f"第 {position} 个部件应为“{slots[position - 1]['slotName']}”，请检查二维码",
                409,
            )
        selected_labels.append(label)

    code_set = database.execute(
        "SELECT id FROM product_code_sets WHERE machine_id = ?", (record["machine_id"],)
    ).fetchone()
    if code_set:
        expected_rows = database.execute(
            """
            SELECT position, part_label_id FROM product_code_set_parts
            WHERE product_code_set_id = ? ORDER BY position
            """,
            (code_set["id"],),
        ).fetchall()
        if [item["id"] for item in selected_labels] != [item["part_label_id"] for item in expected_rows]:
            raise ApiError("部件码与该产品主码不对应，请使用同一套二维码", 409)

    before_codes = [
        row["identification_code"]
        for row in database.execute(
            """
            SELECT pl.identification_code
            FROM trace_record_parts trp JOIN part_labels pl ON pl.id = trp.part_label_id
            WHERE trp.trace_record_id = ? ORDER BY trp.position
            """,
            (record_id,),
        ).fetchall()
    ]
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute("DELETE FROM trace_record_parts WHERE trace_record_id = ?", (record_id,))
        database.executemany(
            """
            INSERT INTO trace_record_parts(trace_record_id, position, part_label_id)
            VALUES (?, ?, ?)
            """,
            [
                (record_id, position, label["id"])
                for position, label in enumerate(selected_labels, start=1)
            ],
        )
        timestamp = current_app.config["NOW_PROVIDER"]()
        database.execute(
            """
            UPDATE trace_records
            SET remarks = ?, status = 'ASSEMBLED', status_reason = '',
                status_updated_at = ?, status_updated_by_user_id = ?
            WHERE id = ?
            """,
            (remarks, timestamp, current_actor_id(), record_id),
        )
        record_audit_event(
            database,
            "TRACE_RECORD_CORRECTED",
            "TRACE_RECORD",
            record["trace_no"],
            related_object_code=record["sn"],
            payload={
                "before": before_codes,
                "after": part_codes,
                "remarks": remarks,
                "statusResetFrom": record["status"],
                "statusResetTo": "ASSEMBLED",
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    updated = next(
        item for item in fetch_records(database, limit=1000) if item["id"] == record_id
    )
    return success(updated)


@records_bp.delete("/api/records/<int:record_id>")
def delete_record(record_id: int):
    require_capability(Capability.RECORD_DELETE)
    database = get_db()
    record = editable_record(database, record_id)
    reason = clean_text(
        (request.get_json(silent=True) or {}).get("reason"),
        "删除原因",
        required=True,
        max_length=200,
    )
    part_codes = [
        row["identification_code"]
        for row in database.execute(
            """
            SELECT pl.identification_code
            FROM trace_record_parts trp JOIN part_labels pl ON pl.id = trp.part_label_id
            WHERE trp.trace_record_id = ? ORDER BY trp.position
            """,
            (record_id,),
        ).fetchall()
    ]
    database.execute("BEGIN IMMEDIATE")
    try:
        record_audit_event(
            database,
            "TRACE_RECORD_DELETED",
            "TRACE_RECORD",
            record["trace_no"],
            related_object_code=record["sn"],
            reason=reason,
            payload={"parts": part_codes},
        )
        database.execute("DELETE FROM trace_records WHERE id = ?", (record_id,))
        database.commit()
    except Exception:
        database.rollback()
        raise
    return success({"id": record_id, "traceNo": record["trace_no"]})


@records_bp.get("/api/genealogy")
def get_genealogy():
    require_capability(Capability.TRACE_VIEW)
    code = clean_text(request.args.get("code"), "查询识别码", required=True, max_length=120).upper()
    database = get_db()
    machine = database.execute(
        """
        SELECT m.*, pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name,
               tp.version AS trace_plan_version, tp.name AS trace_plan_name,
               EXISTS(SELECT 1 FROM trace_records tr WHERE tr.machine_id = m.id) AS traced,
               NULL AS reserved_station
        FROM machines m
        LEFT JOIN product_models pm ON pm.id = m.product_model_id
        LEFT JOIN product_families pf ON pf.id = pm.product_family_id
        LEFT JOIN trace_plans tp ON tp.id = m.trace_plan_id
        WHERE m.sn = ? OR m.identification_code = ?
        """,
        (code, code),
    ).fetchone()
    if machine:
        if machine["product_model_id"] is not None:
            require_product_model_access(machine["product_model_id"])
        records = [
            record for record in fetch_records(database, search=machine["sn"], limit=500)
            if record["machine"]["id"] == machine["id"]
        ]
        events = database.execute(
            """
            SELECT * FROM audit_events
            WHERE object_code IN (?, ?) OR related_object_code = ?
            ORDER BY occurred_at, id
            """,
            (machine["sn"], machine["identification_code"], machine["sn"]),
        ).fetchall()
        return success(
            {
                "queryType": "MACHINE",
                "direction": "BACKWARD",
                "machine": machine_dict(machine),
                "tracePlan": get_plan_summary(database, machine["trace_plan_id"]),
                "records": records,
                "events": [audit_event_dict(event) for event in events],
            }
        )

    label = database.execute(
        """
        SELECT pl.*, pt.part_code, pt.name AS part_name,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at, plb.generated_by AS batch_generated_by,
               EXISTS(SELECT 1 FROM trace_record_parts trp WHERE trp.part_label_id = pl.id) AS used,
               NULL AS reserved_station
        FROM part_labels pl
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE pl.identification_code = ?
        """,
        (code,),
    ).fetchone()
    if label:
        label_product = database.execute(
            """
            SELECT pcs.product_model_id
            FROM product_code_set_parts pcsp
            JOIN product_code_sets pcs ON pcs.id = pcsp.product_code_set_id
            WHERE pcsp.part_label_id = ?
            """,
            (label["id"],),
        ).fetchone()
        if label_product:
            require_product_model_access(label_product["product_model_id"])
        else:
            supplier_id = database.execute(
                "SELECT supplier_id FROM part_types WHERE id = ?", (label["part_type_id"],)
            ).fetchone()["supplier_id"]
            require_supplier_access(supplier_id)
        records = fetch_records(database, search=label["identification_code"], limit=500)
        events = database.execute(
            """
            SELECT * FROM audit_events
            WHERE object_code = ? OR related_object_code = ?
            ORDER BY occurred_at, id
            """,
            (label["identification_code"], label["identification_code"]),
        ).fetchall()
        return success(
            {
                "queryType": "PART_LABEL",
                "direction": "FORWARD",
                "partLabel": part_label_dict(label),
                "records": records,
                "events": [audit_event_dict(event) for event in events],
            }
        )
    raise ApiError("未找到该成品或部件识别码", 404)


@records_bp.get("/api/records/export.xlsx")
def export_records():
    search = clean_text(request.args.get("search"), "搜索内容", max_length=100)
    records = fetch_records(
        get_db(), search=search, limit=100000, completed_by_user_id=current_operator_id()
    )
    max_parts = max([DEFAULT_GENERIC_PART_COUNT] + [len(record["parts"]) for record in records])
    headers = [
        "序号", "记录单号", "状态", "产品分类", "成品 SN", "产品型号", "生产日期",
        "成品识别码", "BOM 名称", "BOM 版本",
    ]
    for position in range(1, max_parts + 1):
        headers.extend(
            [
                f"部件{position}识别码",
                f"部件{position}分类编码",
                f"部件{position}分类名称",
                f"部件{position}编码",
                f"部件{position}名称",
                f"部件{position}供应商编码",
                f"部件{position}供应商",
                f"部件{position}编码批次",
                f"部件{position}批次生成时间",
                f"部件{position}生产批次",
                f"部件{position}供应商批次",
                f"部件{position}供应商单件序列号",
                f"部件{position}生产日期",
                f"部件{position}检验状态",
                f"部件{position}录入人",
                f"部件{position}单码录入时间",
                f"部件{position}备注",
            ]
        )
    headers.extend(["工位", "工位标识", "操作员", "归档时间"])
    rows: list[list[object]] = []
    for index, record in enumerate(records, 1):
        row: list[object] = [
            index,
            record["traceNo"],
            record["status"],
            record["machine"]["productFamilyName"] or "历史产品",
            record["machine"]["sn"],
            record["machine"]["model"],
            record["machine"]["productionDate"],
            record["machine"]["identificationCode"],
            record["tracePlan"]["name"] if record["tracePlan"] else "通用流程",
            record["tracePlan"]["version"] if record["tracePlan"] else "-",
        ]
        parts_by_position = {part["position"]: part for part in record["parts"]}
        for position in range(1, max_parts + 1):
            part = parts_by_position.get(position)
            if part:
                row.extend(
                    [
                        part["identificationCode"],
                        part["categoryCode"],
                        part["categoryName"],
                        part["partCode"],
                        part["partName"],
                        part["supplierCode"],
                        part["supplierName"],
                        part["batchCode"] or "",
                        part["batchGeneratedAt"] or "",
                        part["lotNo"],
                        part["supplierBatchNo"],
                        part["sourceSerialNo"],
                        part["productionDate"],
                        part["inspectionStatus"],
                        part["enteredBy"],
                        part["enteredAt"] or "",
                        part["remarks"],
                    ]
                )
            else:
                row.extend([""] * 17)
        row.extend(
            [
                record["stationName"],
                record["stationId"],
                record["operatorName"],
                record["completedAt"],
            ]
        )
        rows.append(row)
    workbook = build_traceability_xlsx(headers, rows)
    filename = f"产品记录_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        BytesIO(workbook),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
