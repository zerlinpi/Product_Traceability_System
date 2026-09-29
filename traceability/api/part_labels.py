"""Part labels and the BOMs they fill — HTTP layer.

The per-part side of the legacy per-unit flow:

* ``/api/trace-plans`` lists and activates BOMs (trace plans): which part type
  each position of a product takes.
* ``/api/part-label-batches`` and ``/api/part-labels`` generate, list, annotate
  and print the unique part codes (``PTS:P:...``) scanned into those positions,
  generated in batches of up to 100 with an SVG zip per batch.

The BOM read side (``trace_plan_dict`` and the slot cap) lives in
``traceability/trace_plans.py`` because products, the scan station and record
correction read it too.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, Response, request, send_file

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_operator_id,
    require_admin,
    require_capability,
    require_supplier_access,
)
from traceability.capabilities import Capability
from traceability.codes import (
    make_qr_svg,
    new_label_batch_code,
    new_part_identification_code,
    normalize_entity_code,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import part_label_batch_dict, part_label_dict
from traceability.trace_plans import MAX_REQUIRED_PARTS, trace_plan_dict
from traceability.validators import clean_text, csv_safe_row, now_iso

part_labels_bp = Blueprint("part_labels", __name__)


@part_labels_bp.get("/api/trace-plans")
def list_trace_plans():
    database = get_db()
    operator_id = current_operator_id()
    rows = database.execute(
        """
        SELECT tp.*, pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name,
               (SELECT COUNT(*) FROM machines m WHERE m.trace_plan_id = tp.id) AS used_machine_count
        FROM trace_plans tp
        LEFT JOIN product_models pm ON pm.id = tp.product_model_id
        LEFT JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE (? IS NULL OR EXISTS(
            SELECT 1 FROM user_product_model_permissions permission
            WHERE permission.user_id = ? AND permission.product_model_id = tp.product_model_id
        ))
        ORDER BY pf.product_code, tp.model_code, tp.created_at DESC, tp.id DESC
        """,
        (operator_id, operator_id),
    ).fetchall()
    return success([trace_plan_dict(database, row) for row in rows])


@part_labels_bp.post("/api/trace-plans")
def create_trace_plan():
    require_admin()
    payload = request.get_json(silent=True) or {}
    database = get_db()
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
        model_code_input = normalize_entity_code(payload.get("modelCode"), "产品型号编码")
        product_model = database.execute(
            "SELECT * FROM product_models WHERE model_code = ? COLLATE NOCASE AND active = 1",
            (model_code_input,),
        ).fetchone()
    if not product_model:
        raise ApiError("产品型号不存在或已停用，请先由管理员维护产品型号")
    product_model_id = product_model["id"]
    model_code = product_model["model_code"]
    name = clean_text(payload.get("name"), "BOM 名称", required=True, max_length=100)
    version = clean_text(payload.get("version"), "BOM 版本", required=True, max_length=24).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,23}", version):
        raise ApiError("BOM 版本只能包含字母、数字、点、下划线或短横线")
    raw_slots = payload.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise ApiError("BOM 至少需要一个部件槽位")
    expanded_slots: list[tuple[str, int]] = []
    for index, raw_slot in enumerate(raw_slots, 1):
        if not isinstance(raw_slot, dict):
            raise ApiError(f"第 {index} 个 BOM 槽位格式无效")
        slot_name = clean_text(raw_slot.get("slotName"), f"第 {index} 个槽位名称", required=True, max_length=80)
        try:
            part_type_id = int(raw_slot.get("partTypeId"))
            quantity = int(raw_slot.get("quantity", 1))
        except (TypeError, ValueError):
            raise ApiError(f"第 {index} 个槽位的部件类型或数量无效") from None
        if not 1 <= quantity <= MAX_REQUIRED_PARTS:
            raise ApiError(f"第 {index} 个槽位数量需在 1-{MAX_REQUIRED_PARTS} 之间")
        for quantity_index in range(1, quantity + 1):
            expanded_name = slot_name if quantity == 1 else f"{slot_name} {quantity_index}/{quantity}"
            expanded_slots.append((expanded_name, part_type_id))
    if len(expanded_slots) > MAX_REQUIRED_PARTS:
        raise ApiError(f"一个 BOM 最多包含 {MAX_REQUIRED_PARTS} 个实物部件")

    valid_part_ids = {
        row["id"] for row in database.execute(
            f"SELECT id FROM part_types WHERE active = 1 AND id IN ({','.join('?' for _ in expanded_slots)})",
            [part_type_id for _, part_type_id in expanded_slots],
        ).fetchall()
    }
    if any(part_type_id not in valid_part_ids for _, part_type_id in expanded_slots):
        raise ApiError("BOM 中存在无效或已停用的部件类型")

    timestamp = now_iso()
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            "UPDATE trace_plans SET status = 'ARCHIVED' WHERE product_model_id = ? AND status = 'ACTIVE'",
            (product_model_id,),
        )
        cursor = database.execute(
            """
            INSERT INTO trace_plans(
                model_code, product_model_id, name, version, status, created_at, activated_at
            ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (model_code, product_model_id, name, version, timestamp, timestamp),
        )
        plan_id = cursor.lastrowid
        for position, (slot_name, part_type_id) in enumerate(expanded_slots, 1):
            database.execute(
                """
                INSERT INTO trace_plan_slots(trace_plan_id, position, slot_name, part_type_id)
                VALUES (?, ?, ?, ?)
                """,
                (plan_id, position, slot_name, part_type_id),
            )
        record_audit_event(
            database,
            "TRACE_PLAN_ACTIVATED",
            "TRACE_PLAN",
            f"{model_code}@{version}",
            payload={"name": name, "slotCount": len(expanded_slots)},
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute(
        """
        SELECT tp.*, pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name, 0 AS used_machine_count
        FROM trace_plans tp
        JOIN product_models pm ON pm.id = tp.product_model_id
        JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE tp.id = ?
        """,
        (plan_id,),
    ).fetchone()
    return success(trace_plan_dict(database, row), 201)


@part_labels_bp.get("/api/part-label-batches")
def list_part_label_batches():
    search = clean_text(request.args.get("search"), "搜索内容", max_length=100)
    wildcard = f"%{search}%"
    operator_id = current_operator_id()
    rows = get_db().execute(
        """
        SELECT plb.*, pt.part_code, pt.name AS part_name,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               COUNT(pl.id) AS generated_count,
               SUM(CASE WHEN pl.entered_at IS NOT NULL THEN 1 ELSE 0 END) AS data_entered_count
        FROM part_label_batches plb
        JOIN part_types pt ON pt.id = plb.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_labels pl ON pl.label_batch_id = plb.id
        WHERE (? = '' OR plb.batch_code LIKE ? OR pt.part_code LIKE ? OR pt.name LIKE ?
               OR s.supplier_code LIKE ? OR s.name LIKE ? OR plb.lot_no LIKE ?
               OR plb.supplier_batch_no LIKE ?)
          AND (? IS NULL OR EXISTS(
                SELECT 1 FROM user_supplier_permissions permission
                WHERE permission.user_id = ? AND permission.supplier_id = pt.supplier_id
          ))
        GROUP BY plb.id
        ORDER BY plb.generated_at DESC, plb.id DESC LIMIT 500
        """,
        (
            search, wildcard, wildcard, wildcard, wildcard, wildcard, wildcard, wildcard,
            operator_id, operator_id,
        ),
    ).fetchall()
    return success([part_label_batch_dict(row) for row in rows])


@part_labels_bp.get("/api/part-label-batches/<int:batch_id>")
def get_part_label_batch(batch_id: int):
    require_capability(Capability.TRACE_VIEW)
    database = get_db()
    batch = database.execute(
        """
        SELECT plb.*, pt.part_code, pt.name AS part_name, pt.supplier_id,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               COUNT(pl.id) AS generated_count,
               SUM(CASE WHEN pl.entered_at IS NOT NULL THEN 1 ELSE 0 END) AS data_entered_count
        FROM part_label_batches plb
        JOIN part_types pt ON pt.id = plb.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_labels pl ON pl.label_batch_id = plb.id
        WHERE plb.id = ?
        GROUP BY plb.id
        """,
        (batch_id,),
    ).fetchone()
    if not batch:
        raise ApiError("编码批次不存在", 404)
    require_supplier_access(batch["supplier_id"])
    labels = database.execute(
        """
        SELECT pl.*, pt.part_code, pt.name AS part_name,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at,
               plb.generated_by AS batch_generated_by,
               EXISTS(SELECT 1 FROM trace_record_parts trp WHERE trp.part_label_id = pl.id) AS used,
               NULL AS reserved_station
        FROM part_labels pl
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE pl.label_batch_id = ?
        ORDER BY pl.id
        """,
        (batch_id,),
    ).fetchall()
    return success(
        {
            "batch": part_label_batch_dict(batch),
            "labels": [part_label_dict(row) for row in labels],
        }
    )


@part_labels_bp.get("/api/part-label-batches/<int:batch_id>/qrcodes.zip")
def download_part_label_batch_qrcodes(batch_id: int):
    require_capability(Capability.TRACE_VIEW)
    database = get_db()
    batch = database.execute(
        """
        SELECT plb.batch_code, pt.supplier_id
        FROM part_label_batches plb
        JOIN part_types pt ON pt.id = plb.part_type_id
        WHERE plb.id = ?
        """,
        (batch_id,),
    ).fetchone()
    if not batch:
        raise ApiError("编码批次不存在", 404)
    require_supplier_access(batch["supplier_id"])
    labels = database.execute(
        """
        SELECT identification_code, source_serial_no, created_at
        FROM part_labels WHERE label_batch_id = ? ORDER BY id
        """,
        (batch_id,),
    ).fetchall()
    output = BytesIO()
    manifest = StringIO(newline="")
    writer = csv.writer(manifest)
    writer.writerow(["序号", "二维码文件", "部件唯一识别码", "供应商单件序列号", "生成时间"])
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for index, label in enumerate(labels, 1):
            filename = f"QR_{index:04d}.svg"
            archive.writestr(filename, make_qr_svg(label["identification_code"]))
            # Supplier serials are typed text: keep them inert in Excel.
            writer.writerow(
                csv_safe_row(
                    [
                        index, filename, label["identification_code"],
                        label["source_serial_no"], label["created_at"],
                    ]
                )
            )
        archive.writestr("编码清单.csv", "\ufeff" + manifest.getvalue())
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{batch['batch_code']}_二维码.zip",
        mimetype="application/zip",
    )


@part_labels_bp.get("/api/part-labels")
def list_part_labels():
    search = clean_text(request.args.get("search"), "搜索内容", max_length=100)
    wildcard = f"%{search}%"
    operator_id = current_operator_id()
    rows = get_db().execute(
        """
        SELECT pl.*, pt.part_code, pt.name AS part_name,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at, plb.generated_by AS batch_generated_by,
               EXISTS(SELECT 1 FROM trace_record_parts trp WHERE trp.part_label_id = pl.id) AS used,
               (SELECT ss.station_name FROM scan_session_items ssi
                JOIN scan_sessions ss ON ss.station_id = ssi.station_id
                WHERE ssi.part_label_id = pl.id LIMIT 1) AS reserved_station
        FROM part_labels pl
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE (? = '' OR pl.identification_code LIKE ? OR pt.part_code LIKE ? OR pt.name LIKE ?
               OR s.supplier_code LIKE ? OR s.name LIKE ? OR pl.lot_no LIKE ? OR pl.supplier_batch_no LIKE ?
               OR pl.source_serial_no LIKE ? OR plb.batch_code LIKE ?)
          AND (? IS NULL OR EXISTS(
                SELECT 1 FROM user_supplier_permissions permission
                WHERE permission.user_id = ? AND permission.supplier_id = pt.supplier_id
          ))
        ORDER BY pl.created_at DESC, pl.id DESC LIMIT 500
        """,
        (
            search, wildcard, wildcard, wildcard, wildcard, wildcard,
            wildcard, wildcard, wildcard, wildcard, operator_id, operator_id,
        ),
    ).fetchall()
    return success([part_label_dict(row) for row in rows])


@part_labels_bp.post("/api/part-labels")
def create_part_labels():
    require_admin()
    payload = request.get_json(silent=True) or {}
    try:
        part_type_id = int(payload.get("partTypeId"))
        quantity = int(payload.get("quantity", 1))
    except (TypeError, ValueError):
        raise ApiError("部件类型或生成数量无效") from None
    if not 1 <= quantity <= 100:
        raise ApiError("单次生成数量需在 1-100 之间")
    lot_no = clean_text(payload.get("lotNo"), "生产批次", max_length=80)
    supplier_batch_no = clean_text(payload.get("supplierBatchNo"), "供应商批次", max_length=80)
    production_date = clean_text(payload.get("productionDate"), "部件生产日期", max_length=20)
    actor_user_id = current_actor_id()
    submitted_generator = clean_text(payload.get("generatedBy"), "生成操作员", max_length=80)
    generated_by = (
        current_actor_name(submitted_generator)
        if actor_user_id is not None
        else submitted_generator
    )
    database = get_db()
    part_type = database.execute(
        "SELECT id, part_code, supplier_id FROM part_types WHERE id = ? AND active = 1",
        (part_type_id,),
    ).fetchone()
    if not part_type:
        raise ApiError("部件类型不存在或已停用")
    require_supplier_access(part_type["supplier_id"])
    created_ids: list[int] = []
    generated_at = now_iso()
    batch_time = datetime.now().strftime("%Y%m%d%H%M%S")
    database.execute("BEGIN IMMEDIATE")
    try:
        for batch_attempt in range(8):
            batch_code = new_label_batch_code(batch_time)
            try:
                batch_cursor = database.execute(
                    """
                    INSERT INTO part_label_batches(
                        batch_code, part_type_id, lot_no, supplier_batch_no,
                        quantity, production_date, generated_by, generated_by_user_id, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_code, part_type_id, lot_no, supplier_batch_no,
                        quantity, production_date, generated_by, actor_user_id, generated_at,
                    ),
                )
                break
            except sqlite3.IntegrityError as error:
                if "part_label_batches.batch_code" not in str(error) or batch_attempt == 7:
                    raise
        label_batch_id = batch_cursor.lastrowid
        record_audit_event(
            database,
            "PART_LABEL_BATCH_GENERATED",
            "PART_LABEL_BATCH",
            batch_code,
            operator_name=generated_by,
            payload={
                "partTypeId": part_type_id,
                "partCode": part_type["part_code"],
                "quantity": quantity,
                "lotNo": lot_no,
                "supplierBatchNo": supplier_batch_no,
                "productionDate": production_date,
            },
            occurred_at=generated_at,
        )
        for _ in range(quantity):
            for code_attempt in range(8):
                identification_code = new_part_identification_code()
                try:
                    cursor = database.execute(
                        """
                        INSERT INTO part_labels(
                            identification_code, part_type_id, label_batch_id, lot_no,
                            supplier_batch_no, production_date, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identification_code, part_type_id, label_batch_id, lot_no,
                            supplier_batch_no, production_date, generated_at,
                        ),
                    )
                    break
                except sqlite3.IntegrityError as error:
                    if "part_labels.identification_code" not in str(error) or code_attempt == 7:
                        raise
            created_ids.append(cursor.lastrowid)
            record_audit_event(
                database,
                "PART_LABEL_COMMISSIONED",
                "PART_LABEL",
                identification_code,
                payload={
                    "partTypeId": part_type_id,
                    "partCode": part_type["part_code"],
                    "batchCode": batch_code,
                    "lotNo": lot_no,
                    "supplierBatchNo": supplier_batch_no,
                },
                occurred_at=generated_at,
            )
        database.commit()
    except Exception:
        database.rollback()
        raise
    placeholders = ",".join("?" for _ in created_ids)
    rows = database.execute(
        f"""
        SELECT pl.*, pt.part_code, pt.name AS part_name,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at, plb.generated_by AS batch_generated_by,
               0 AS used, NULL AS reserved_station
        FROM part_labels pl JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE pl.id IN ({placeholders}) ORDER BY pl.id
        """,
        created_ids,
    ).fetchall()
    return success([part_label_dict(row) for row in rows], 201)


@part_labels_bp.put("/api/part-labels/<int:label_id>")
def update_part_label_data(label_id: int):
    require_admin()
    payload = request.get_json(silent=True) or {}
    source_serial_no = clean_text(payload.get("sourceSerialNo"), "供应商单件序列号", max_length=100)
    production_date = clean_text(payload.get("productionDate"), "部件生产日期", max_length=20)
    inspection_status = clean_text(
        payload.get("inspectionStatus") or "PENDING", "检验状态", max_length=16
    ).upper()
    if inspection_status not in {"PENDING", "PASS", "FAIL"}:
        raise ApiError("检验状态无效")
    remarks = clean_text(payload.get("remarks"), "备注", max_length=500)
    actor_user_id = current_actor_id()
    submitted_entered_by = clean_text(
        payload.get("enteredBy"), "录入人", required=actor_user_id is None, max_length=80
    )
    entered_by = (
        current_actor_name(submitted_entered_by)
        if actor_user_id is not None
        else submitted_entered_by
    )
    database = get_db()
    existing = database.execute(
        """
        SELECT pl.*, pt.supplier_id
        FROM part_labels pl
        JOIN part_types pt ON pt.id = pl.part_type_id
        WHERE pl.id = ?
        """,
        (label_id,),
    ).fetchone()
    if not existing:
        raise ApiError("部件标签不存在", 404)
    require_supplier_access(existing["supplier_id"])
    updated_at = now_iso()
    entered_at = existing["entered_at"] or updated_at
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            """
            UPDATE part_labels
            SET source_serial_no = ?, production_date = ?, inspection_status = ?,
                remarks = ?, entered_by = ?, entered_by_user_id = ?,
                entered_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                source_serial_no, production_date, inspection_status, remarks,
                entered_by, actor_user_id, entered_at, updated_at, label_id,
            ),
        )
        record_audit_event(
            database,
            "PART_LABEL_DATA_ENTERED",
            "PART_LABEL",
            existing["identification_code"],
            operator_name=entered_by,
            payload={
                "sourceSerialNo": source_serial_no,
                "productionDate": production_date,
                "inspectionStatus": inspection_status,
                "remarks": remarks,
            },
            occurred_at=updated_at,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute(
        """
        SELECT pl.*, pt.part_code, pt.name AS part_name,
               pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at, plb.generated_by AS batch_generated_by,
               EXISTS(SELECT 1 FROM trace_record_parts trp WHERE trp.part_label_id = pl.id) AS used,
               (SELECT ss.station_name FROM scan_session_items ssi
                JOIN scan_sessions ss ON ss.station_id = ssi.station_id
                WHERE ssi.part_label_id = pl.id LIMIT 1) AS reserved_station
        FROM part_labels pl
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE pl.id = ?
        """,
        (label_id,),
    ).fetchone()
    return success(part_label_dict(row))


@part_labels_bp.get("/api/part-labels/<int:label_id>/qr")
def part_label_qr(label_id: int):
    require_capability(Capability.TRACE_VIEW)
    row = get_db().execute(
        """
        SELECT pl.identification_code, pt.supplier_id
        FROM part_labels pl
        JOIN part_types pt ON pt.id = pl.part_type_id
        WHERE pl.id = ?
        """,
        (label_id,),
    ).fetchone()
    if not row:
        raise ApiError("部件标签不存在", 404)
    require_supplier_access(row["supplier_id"])
    return Response(make_qr_svg(row["identification_code"]), mimetype="image/svg+xml")
