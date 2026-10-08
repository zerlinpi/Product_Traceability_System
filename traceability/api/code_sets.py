"""Per-unit product code sets (产品套码) — HTTP layer.

Generating QR sets for a product, one set per unit (a main code plus a part code
per BOM slot) grouped into generation batches; listing sets and batches; and
downloading the SVG codes as a zip for one set, one batch or a whole product.
Generation draws the bound supplier inventory down in the same transaction.

Treadmill products are refused (see ``traceability/legacy_scan.py``): they moved
to batch-level traceability. Historical sets stay readable and downloadable.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from datetime import datetime
from io import BytesIO, StringIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, current_app, request, send_file

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_operator_id,
    require_admin,
    require_product_model_access,
)
from traceability.code_sets import code_set_dicts, product_code_set_dict
from traceability.codes import (
    machine_identification_code,
    make_qr_svg,
    normalize_entity_code,
    normalize_sn,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.legacy_scan import (
    LEGACY_ENTRY_DISABLED_MESSAGE,
    is_treadmill_product_model,
)
from traceability.responses import success
from traceability.serializers import product_code_batch_dict
from traceability.validators import csv_safe_row, safe_archive_name

code_sets_bp = Blueprint("code_sets", __name__)


@code_sets_bp.post("/api/products/<int:product_model_id>/code-sets")
def create_product_code_sets(product_model_id: int):
    require_admin()
    payload = request.get_json(silent=True) or {}
    prefix = normalize_entity_code(payload.get("prefix"), "二维码前缀")
    try:
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError) as error:
        raise ApiError("生成数量无效") from error
    if quantity < 1:
        raise ApiError("单次生成数量至少为 1")
    if quantity > 1000:
        raise ApiError("单次最多生成 1000 套，请分批生成")
    database = get_db()
    product = database.execute(
        "SELECT * FROM product_models WHERE id = ? AND active = 1", (product_model_id,)
    ).fetchone()
    if not product:
        raise ApiError("产品不存在或已停用", 404)
    # Requirement 7.3: per-unit code-set generation is retired for treadmill
    # products. Reject before opening a write transaction so no per-unit
    # ``product_code_sets`` / ``machines`` rows are created. Non-treadmill
    # products keep generating code sets as before (Requirement 7.5).
    if is_treadmill_product_model(database, product_model_id):
        raise ApiError(LEGACY_ENTRY_DISABLED_MESSAGE, 409)
    plan = database.execute(
        """
        SELECT * FROM trace_plans
        WHERE product_model_id = ? AND status = 'ACTIVE'
        ORDER BY activated_at DESC, id DESC LIMIT 1
        """,
        (product_model_id,),
    ).fetchone()
    if not plan:
        raise ApiError("该产品尚未配置部件清单", 409)
    slots = database.execute(
        """
        SELECT slot.*, pt.id AS part_type_id, pt.part_code, pt.name AS part_name,
               s.name AS supplier_name, sib.batch_no AS inventory_batch_no,
               sib.production_date AS inventory_production_date
        FROM trace_plan_slots slot
        JOIN part_types pt ON pt.id = slot.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN supplier_inventory_batches sib
               ON sib.id = slot.supplier_inventory_batch_id
        WHERE slot.trace_plan_id = ? ORDER BY slot.position
        """,
        (plan["id"],),
    ).fetchall()
    if not slots:
        raise ApiError("该产品尚未配置部件", 409)
    timestamp = current_app.config["NOW_PROVIDER"]()
    try:
        date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        date = datetime.now().astimezone()
    date_code = date.strftime("%Y%m%d")
    production_date = date.strftime("%Y%m%d")
    actor_user_id = current_actor_id()
    generated_ids: list[int] = []
    database.execute("BEGIN IMMEDIATE")
    try:
        next_sequence = database.execute(
            """
            SELECT COALESCE(MAX(daily_sequence), 0) + 1 AS next_sequence
            FROM product_code_sets WHERE prefix = ? COLLATE NOCASE AND date_code = ?
            """,
            (prefix, date_code),
        ).fetchone()["next_sequence"]
        if next_sequence + quantity - 1 > 9999:
            raise ApiError("该前缀今日流水号已用尽，请更换前缀", 409)
        end_sequence = next_sequence + quantity - 1
        generation_batch_code = f"{prefix}-{date_code}-{next_sequence:04d}-{end_sequence:04d}"
        generation_batch_cursor = database.execute(
            """
            INSERT INTO product_code_batches(
                batch_code, product_model_id, trace_plan_id, prefix, date_code,
                quantity, start_sequence, end_sequence,
                generated_by_user_id, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_batch_code, product_model_id, plan["id"], prefix,
                date_code, quantity, next_sequence, end_sequence,
                actor_user_id, timestamp,
            ),
        )
        generation_batch_id = generation_batch_cursor.lastrowid

        inventory_requirements: dict[int, int] = defaultdict(int)
        for slot in slots:
            if slot["supplier_inventory_batch_id"] is not None:
                inventory_requirements[slot["supplier_inventory_batch_id"]] += quantity
        for inventory_batch_id, required_quantity in inventory_requirements.items():
            inventory = database.execute(
                """
                SELECT sib.*, pt.name AS part_name
                FROM supplier_inventory_batches sib
                JOIN part_types pt ON pt.id = sib.part_type_id
                WHERE sib.id = ?
                """,
                (inventory_batch_id,),
            ).fetchone()
            if not inventory or not inventory["active"]:
                raise ApiError("产品绑定的供应商批次已停用，请先编辑产品", 409)
            if inventory["quantity_available"] < required_quantity:
                raise ApiError(
                    f"{inventory['part_name']} / {inventory['batch_no']} 库存不足："
                    f"需要 {required_quantity}，可用 {inventory['quantity_available']}",
                    409,
                )
            balance_after = inventory["quantity_available"] - required_quantity
            database.execute(
                """
                UPDATE supplier_inventory_batches
                SET quantity_available = ?, updated_at = ? WHERE id = ?
                """,
                (balance_after, timestamp, inventory_batch_id),
            )
            database.execute(
                """
                INSERT INTO supplier_inventory_movements(
                    inventory_batch_id, movement_type, quantity_change,
                    balance_after, product_model_id, product_code_batch_id,
                    actor_user_id, reason, occurred_at
                ) VALUES (?, 'ISSUE', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inventory_batch_id, -required_quantity, balance_after,
                    product_model_id, generation_batch_id, actor_user_id,
                    f"生成 {generation_batch_code} 产品二维码",
                    timestamp,
                ),
            )
        for quantity_index in range(quantity):
            sequence = next_sequence + quantity_index
            set_code = normalize_sn(f"{prefix}-{date_code}-{sequence:04d}")
            machine_cursor = database.execute(
                """
                INSERT INTO machines(
                    sn, model, product_model_id, production_date,
                    trace_plan_id, identification_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    set_code,
                    product["model_code"],
                    product_model_id,
                    production_date,
                    plan["id"],
                    machine_identification_code(set_code),
                    timestamp,
                ),
            )
            set_cursor = database.execute(
                """
                INSERT INTO product_code_sets(
                    generation_batch_id, product_model_id, trace_plan_id, machine_id,
                    prefix, date_code, daily_sequence, set_code,
                    generated_by_user_id, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_batch_id, product_model_id,
                    plan["id"],
                    machine_cursor.lastrowid,
                    prefix,
                    date_code,
                    sequence,
                    set_code,
                    actor_user_id,
                    timestamp,
                ),
            )
            generated_ids.append(set_cursor.lastrowid)
            for slot in slots:
                part_code = f"PTS:P:{set_code}:{slot['position']:02d}"
                part_cursor = database.execute(
                    """
                    INSERT INTO part_labels(
                        identification_code, part_type_id, lot_no,
                        supplier_batch_no, production_date,
                        inspection_status, entered_by, entered_by_user_id,
                        entered_at, updated_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                    """,
                    (
                        part_code,
                        slot["part_type_id"],
                        set_code,
                        slot["inventory_batch_no"] or "",
                        slot["inventory_production_date"] or production_date,
                        current_actor_name("系统管理员"),
                        actor_user_id,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                database.execute(
                    """
                    INSERT INTO product_code_set_parts(
                        product_code_set_id, position, slot_name, part_label_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (set_cursor.lastrowid, slot["position"], slot["slot_name"], part_cursor.lastrowid),
                )
            record_audit_event(
                database,
                "PRODUCT_CODES_GENERATED",
                "PRODUCT_CODE_SET",
                set_code,
                related_object_code=product["model_code"],
                payload={"productModelId": product_model_id, "partCount": len(slots)},
                occurred_at=timestamp,
            )
        record_audit_event(
            database,
            "PRODUCT_CODE_BATCH_GENERATED",
            "PRODUCT_CODE_BATCH",
            generation_batch_code,
            related_object_code=product["model_code"],
            payload={
                "productModelId": product_model_id,
                "quantity": quantity,
                "partCountPerSet": len(slots),
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise

    placeholders = ",".join("?" for _ in generated_ids)
    rows = database.execute(
        f"""
        SELECT pcs.*, pm.name AS product_name,
               m.sn AS machine_sn, m.identification_code AS machine_code
        FROM product_code_sets pcs
        JOIN product_models pm ON pm.id = pcs.product_model_id
        JOIN machines m ON m.id = pcs.machine_id
        WHERE pcs.id IN ({placeholders}) ORDER BY pcs.id
        """,
        generated_ids,
    ).fetchall()
    return success(code_set_dicts(database, rows), 201)


@code_sets_bp.get("/api/product-code-sets")
def list_product_code_sets():
    require_admin()
    database = get_db()
    product_model_value = request.args.get("productModelId")
    parameters: list[Any] = []
    clauses: list[str] = []
    if product_model_value:
        try:
            product_model_id = int(product_model_value)
        except (TypeError, ValueError) as error:
            raise ApiError("产品参数无效") from error
        require_product_model_access(product_model_id)
        clauses.append("pcs.product_model_id = ?")
        parameters.append(product_model_id)
    operator_id = current_operator_id()
    if operator_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM user_product_model_permissions permission "
            "WHERE permission.user_id = ? AND permission.product_model_id = pcs.product_model_id)"
        )
        parameters.append(operator_id)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = database.execute(
        f"""
        SELECT pcs.*, pm.name AS product_name,
               m.sn AS machine_sn, m.identification_code AS machine_code
        FROM product_code_sets pcs
        JOIN product_models pm ON pm.id = pcs.product_model_id
        JOIN machines m ON m.id = pcs.machine_id
        {where_clause}
        ORDER BY pcs.generated_at DESC, pcs.id DESC LIMIT 300
        """,
        parameters,
    ).fetchall()
    return success(code_set_dicts(database, rows))


@code_sets_bp.get("/api/product-code-batches")
def list_product_code_batches():
    require_admin()
    database = get_db()
    product_model_value = request.args.get("productModelId")
    parameters: list[Any] = []
    where_clause = ""
    if product_model_value not in (None, ""):
        try:
            product_model_id = int(product_model_value)
        except (TypeError, ValueError) as error:
            raise ApiError("产品参数无效") from error
        where_clause = "WHERE pcb.product_model_id = ?"
        parameters.append(product_model_id)
    rows = database.execute(
        f"""
        SELECT pcb.*, pm.name AS product_name,
               COALESCE(u.display_name, u.username, '') AS generated_by
        FROM product_code_batches pcb
        JOIN product_models pm ON pm.id = pcb.product_model_id
        LEFT JOIN users u ON u.id = pcb.generated_by_user_id
        {where_clause}
        ORDER BY pcb.generated_at DESC, pcb.id DESC
        """,
        parameters,
    ).fetchall()
    return success([product_code_batch_dict(row) for row in rows])


@code_sets_bp.get("/api/product-code-batches/<int:generation_batch_id>")
def get_product_code_batch(generation_batch_id: int):
    require_admin()
    database = get_db()
    batch = database.execute(
        """
        SELECT pcb.*, pm.name AS product_name,
               COALESCE(u.display_name, u.username, '') AS generated_by
        FROM product_code_batches pcb
        JOIN product_models pm ON pm.id = pcb.product_model_id
        LEFT JOIN users u ON u.id = pcb.generated_by_user_id
        WHERE pcb.id = ?
        """,
        (generation_batch_id,),
    ).fetchone()
    if not batch:
        raise ApiError("二维码生成批次不存在", 404)
    try:
        page = max(1, int(request.args.get("page", "1")))
        page_size = min(100, max(10, int(request.args.get("pageSize", "40"))))
    except (TypeError, ValueError) as error:
        raise ApiError("分页参数无效") from error
    total = database.execute(
        "SELECT COUNT(*) AS n FROM product_code_sets WHERE generation_batch_id = ?",
        (generation_batch_id,),
    ).fetchone()["n"]
    rows = database.execute(
        """
        SELECT pcs.*, pm.name AS product_name,
               m.sn AS machine_sn, m.identification_code AS machine_code
        FROM product_code_sets pcs
        JOIN product_models pm ON pm.id = pcs.product_model_id
        JOIN machines m ON m.id = pcs.machine_id
        WHERE pcs.generation_batch_id = ?
        ORDER BY pcs.daily_sequence
        LIMIT ? OFFSET ?
        """,
        (generation_batch_id, page_size, (page - 1) * page_size),
    ).fetchall()
    return success(
        {
            "batch": product_code_batch_dict(batch),
            "sets": code_set_dicts(database, rows),
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "total": total,
                "totalPages": max(1, (total + page_size - 1) // page_size),
            },
        }
    )


#: How many code sets to read at once when building an export. Large enough that
#: the query count is negligible, small enough that the parts of every set in a
#: long export are never all resident.
ZIP_PART_CHUNK = 200


def write_code_sets_zip(
    database: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    root_folder: str = "",
) -> BytesIO:
    output = BytesIO()
    manifest = StringIO()
    writer = csv.writer(manifest)
    writer.writerow(["生成批次", "产品套码", "类型", "顺序", "名称", "供应商", "识别码"])
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        # Parts are read a chunk of sets at a time. Reading them per row ran a
        # query for every set — 10,001 for a 10,000-code export — while reading
        # every set up front would hold all of them at once. A chunk bounds both.
        cache: dict[int, dict] = {}
        for index, row in enumerate(rows):
            if row["id"] not in cache:
                chunk = rows[index : index + ZIP_PART_CHUNK]
                cache = {item["id"]: item for item in code_set_dicts(database, chunk)}
            data = cache[row["id"]]
            batch_folder = safe_archive_name(
                row["batch_code"] if "batch_code" in row.keys() else "历史批次",
                "历史批次",
            )
            folder_parts = [
                safe_archive_name(part) for part in (root_folder, batch_folder, data["setCode"])
                if part
            ]
            folder = "/".join(folder_parts)
            archive.writestr(
                f"{folder}/00-产品主码.svg",
                make_qr_svg(data["machine"]["identificationCode"]),
            )
            # Product / part / supplier names are typed text: keep them inert
            # when the manifest is opened in Excel.
            writer.writerow(
                csv_safe_row(
                    [batch_folder, data["setCode"], "产品主码", 0, data["productName"], "", data["machine"]["identificationCode"]]
                )
            )
            for part in data["parts"]:
                archive.writestr(
                    f"{folder}/{part['position']:02d}-{safe_archive_name(part['partName'], '产品部件')}.svg",
                    make_qr_svg(part["identificationCode"]),
                )
                writer.writerow(
                    csv_safe_row(
                        [batch_folder, data["setCode"], "产品部件", part["position"], part["partName"], part["supplierName"], part["identificationCode"]]
                    )
                )
        archive.writestr(
            f"{root_folder + '/' if root_folder else ''}二维码清单.csv",
            "\ufeff" + manifest.getvalue(),
        )
    output.seek(0)
    return output


@code_sets_bp.get("/api/product-code-batches/<int:generation_batch_id>/qrcodes.zip")
def download_product_code_batch(generation_batch_id: int):
    require_admin()
    database = get_db()
    batch = database.execute(
        "SELECT * FROM product_code_batches WHERE id = ?", (generation_batch_id,)
    ).fetchone()
    if not batch:
        raise ApiError("二维码生成批次不存在", 404)
    rows = database.execute(
        """
        SELECT pcs.*, pm.name AS product_name,
               m.sn AS machine_sn, m.identification_code AS machine_code,
               pcb.batch_code
        FROM product_code_sets pcs
        JOIN product_models pm ON pm.id = pcs.product_model_id
        JOIN machines m ON m.id = pcs.machine_id
        JOIN product_code_batches pcb ON pcb.id = pcs.generation_batch_id
        WHERE pcs.generation_batch_id = ?
        ORDER BY pcs.daily_sequence
        """,
        (generation_batch_id,),
    ).fetchall()
    output = write_code_sets_zip(database, rows)
    return send_file(
        output,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{batch['batch_code']}-全部二维码.zip",
    )


@code_sets_bp.get("/api/products/<int:product_model_id>/qrcodes.zip")
def download_all_product_qrcodes(product_model_id: int):
    require_admin()
    database = get_db()
    product = database.execute(
        "SELECT * FROM product_models WHERE id = ?", (product_model_id,)
    ).fetchone()
    if not product:
        raise ApiError("产品不存在", 404)
    rows = database.execute(
        """
        SELECT pcs.*, pm.name AS product_name,
               m.sn AS machine_sn, m.identification_code AS machine_code,
               COALESCE(pcb.batch_code, '历史批次') AS batch_code
        FROM product_code_sets pcs
        JOIN product_models pm ON pm.id = pcs.product_model_id
        JOIN machines m ON m.id = pcs.machine_id
        LEFT JOIN product_code_batches pcb ON pcb.id = pcs.generation_batch_id
        WHERE pcs.product_model_id = ?
        ORDER BY pcs.generated_at, pcs.daily_sequence
        """,
        (product_model_id,),
    ).fetchall()
    if not rows:
        raise ApiError("该产品尚未生成二维码", 404)
    output = write_code_sets_zip(database, rows, root_folder=product["model_code"])
    return send_file(
        output,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{product['model_code']}-全部二维码.zip",
    )


@code_sets_bp.get("/api/product-code-sets/<int:code_set_id>/qrcodes.zip")
def download_product_code_set(code_set_id: int):
    require_admin()
    database = get_db()
    row = database.execute(
        """
        SELECT pcs.*, pm.name AS product_name,
               m.sn AS machine_sn, m.identification_code AS machine_code
        FROM product_code_sets pcs
        JOIN product_models pm ON pm.id = pcs.product_model_id
        JOIN machines m ON m.id = pcs.machine_id
        WHERE pcs.id = ?
        """,
        (code_set_id,),
    ).fetchone()
    if not row:
        raise ApiError("二维码套件不存在", 404)
    data = product_code_set_dict(database, row)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("00-产品主码.svg", make_qr_svg(data["machine"]["identificationCode"]))
        manifest = StringIO()
        writer = csv.writer(manifest)
        writer.writerow(["类型", "顺序", "名称", "供应商", "识别码"])
        writer.writerow(
            csv_safe_row(["产品主码", 0, data["productName"], "", data["machine"]["identificationCode"]])
        )
        for part in data["parts"]:
            filename = f"{part['position']:02d}-产品部件.svg"
            archive.writestr(filename, make_qr_svg(part["identificationCode"]))
            writer.writerow(
                csv_safe_row(
                    ["产品部件", part["position"], part["partName"], part["supplierName"], part["identificationCode"]]
                )
            )
        archive.writestr("二维码清单.csv", "\ufeff" + manifest.getvalue())
    output.seek(0)
    return send_file(
        output,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{data['setCode']}-二维码.zip",
    )
