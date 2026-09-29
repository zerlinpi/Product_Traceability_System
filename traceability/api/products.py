"""Products — HTTP layer.

The product catalog: listing, creating and editing products, the extended
profile form (its columns come from ``/api/product-attribute-columns`` so the
client never hardcodes the template) and the 主图 picture upload.

ADMIN manages the full manufacturing product including its BOM; OPERATIONS may
register and maintain their own lightweight products, which are scoped to their
creator. The profile rules and the configuration payload live in
``traceability/products.py``; the writes stay in the routes.

Per-unit QR generation for a product (``/api/products/<id>/code-sets`` and the
``qrcodes.zip`` downloads) lives in ``code_sets.py``.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, request, send_file

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_operator_id,
    current_user,
    require_operations,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.products import (
    PRODUCT_ATTRIBUTE_COLUMNS,
    PRODUCT_ATTRIBUTE_DERIVED_COLUMNS,
    PRODUCT_IMAGE_COLUMNS,
    PRODUCT_IMAGE_EXTENSIONS,
    PRODUCT_IMAGE_MAX_BYTES,
    PRODUCT_IMAGE_MAX_PIXELS,
    PRODUCT_IMAGE_MAX_SIDE,
    PRODUCT_IMAGE_NAME_PATTERN,
    clean_product_attributes,
    cleared_product_attribute_columns,
    list_product_configurations,
    product_attributes_with_defaults,
    product_configuration_dict,
)
from traceability.responses import success
from traceability.trace_plans import MAX_REQUIRED_PARTS
from traceability.validators import (
    clean_text,
    detect_product_image_type,
    image_dimensions,
    parse_bool,
)

products_bp = Blueprint("products", __name__)


def product_image_dir() -> Path:
    """Directory holding uploaded product pictures (next to the database)."""
    directory = Path(current_app.config["DATABASE"]).resolve().parent / "product-images"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@products_bp.post("/api/product-images")
def upload_product_image():
    # Operations register their own products and admins maintain all of
    # them, so both may upload a 主图. The file is validated by signature and
    # stored under a generated name; the caller receives the URL to save in
    # the product profile.
    require_operations()
    upload = request.files.get("file") or request.files.get("image")
    if upload is None or not upload.filename:
        raise ApiError("请选择要上传的图片")
    data = upload.read(PRODUCT_IMAGE_MAX_BYTES + 1)
    if not data:
        raise ApiError("图片内容为空")
    if len(data) > PRODUCT_IMAGE_MAX_BYTES:
        raise ApiError("图片不能超过 5 MB")
    # Signature first (so SVG, HTML or a renamed executable never gets this
    # far), then the pixel size declared in the header.
    extension, mimetype = detect_product_image_type(data)
    width, height = image_dimensions(data, extension)
    if (
        width > PRODUCT_IMAGE_MAX_SIDE
        or height > PRODUCT_IMAGE_MAX_SIDE
        or width * height > PRODUCT_IMAGE_MAX_PIXELS
    ):
        raise ApiError(
            f"图片尺寸过大（{width}×{height}）：最长边不超过 {PRODUCT_IMAGE_MAX_SIDE} 像素，"
            f"总像素不超过 {PRODUCT_IMAGE_MAX_PIXELS // 10_000} 万"
        )
    # A generated name: the client filename never reaches the filesystem, so
    # path traversal and overwriting an existing picture are impossible.
    filename = f"{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(12)}.{extension}"
    target = product_image_dir() / filename
    target.write_bytes(data)
    return success(
        {
            "filename": filename,
            "url": f"/api/product-images/{filename}",
            "contentType": mimetype,
            "size": len(data),
            "width": width,
            "height": height,
        },
        201,
    )


@products_bp.get("/api/product-images/<path:filename>")
def get_product_image(filename: str):
    # Only names this system generated are served, and the path is rebuilt
    # from the storage directory, so no traversal outside it is possible.
    if not PRODUCT_IMAGE_NAME_PATTERN.fullmatch(filename):
        raise ApiError("图片不存在", 404)
    target = product_image_dir() / filename
    if not target.is_file():
        raise ApiError("图片不存在", 404)
    extension = target.suffix.lstrip(".").lower()
    return send_file(target, mimetype=PRODUCT_IMAGE_EXTENSIONS.get(extension, "application/octet-stream"))


@products_bp.get("/api/product-attribute-columns")
def list_product_attribute_columns():
    # The product form builds its inputs from this list so the client never
    # hardcodes the template. Derived columns are reported separately and are
    # rendered read-only (the server always fills them).
    return success(
        {
            "columns": PRODUCT_ATTRIBUTE_COLUMNS,
            "derived": sorted(PRODUCT_ATTRIBUTE_DERIVED_COLUMNS),
            # Columns rendered as an image upload instead of a text input.
            "imageColumns": PRODUCT_IMAGE_COLUMNS,
        }
    )


@products_bp.get("/api/products")
def list_products():
    database = get_db()
    actor = current_user()
    actor_role = actor["role"] if actor else ""
    operator_id = current_operator_id()
    if actor_role == "OPERATIONS":
        # Operations only ever see the products they added themselves.
        rows = database.execute(
            "SELECT * FROM product_models WHERE created_by_user_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (current_actor_id(),),
        ).fetchall()
    elif operator_id is None:
        # Admin (and the auth-disabled test admin) see every product.
        rows = database.execute(
            "SELECT * FROM product_models ORDER BY active DESC, created_at DESC, id DESC"
        ).fetchall()
    else:
        # Warehouse stays scoped to its granted product models.
        rows = database.execute(
            """
            SELECT pm.*
            FROM user_product_model_permissions permission
            JOIN product_models pm ON pm.id = permission.product_model_id
            WHERE permission.user_id = ? AND pm.active = 1
            ORDER BY pm.created_at DESC, pm.id DESC
            """,
            (operator_id,),
        ).fetchall()
    return success(list_product_configurations(database, rows))


@products_bp.post("/api/products")
def create_product():
    # ADMIN keeps the full manufacturing product (with a BOM); OPERATIONS may
    # register their own lightweight products (name + optional SKU, no BOM),
    # which are scoped to their creator.
    require_operations()
    payload = request.get_json(silent=True) or {}
    name = clean_text(payload.get("name"), "产品名称", required=True, max_length=100)
    actor_user_id = current_actor_id()
    # Extended product profile; 创建人 defaults to the saving account.
    attributes = clean_product_attributes(payload.get("attributes"))
    creator_name = current_actor_name("系统管理员")
    component_payloads = payload.get("components")
    if not isinstance(component_payloads, list) or not component_payloads:
        # A product may be a complete, indivisible item with no BOM (some
        # suppliers ship finished goods). Both ADMIN and OPERATIONS may
        # register such a component-less product; it simply has no trace
        # plan and can be purchased/received but not batch-generated.
        database = get_db()
        timestamp = current_app.config["NOW_PROVIDER"]()
        try:
            date_code = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            ).strftime("%Y%m%d")
        except ValueError:
            date_code = datetime.now().astimezone().strftime("%Y%m%d")
        requested_code = clean_text(
            payload.get("modelCode") or payload.get("sku"), "产品编码", max_length=60
        )
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                """
                INSERT OR IGNORE INTO product_families(
                    product_code, name, description, active, created_at, updated_at
                ) VALUES ('OPERATIONS_PRODUCTS', '运营产品', '由运营创建的采购产品', 1, ?, ?)
                """,
                (timestamp, timestamp),
            )
            family_id = database.execute(
                "SELECT id FROM product_families WHERE product_code = 'OPERATIONS_PRODUCTS' COLLATE NOCASE"
            ).fetchone()["id"]
            if requested_code:
                model_code = requested_code.upper()
                if database.execute(
                    "SELECT 1 FROM product_models WHERE model_code = ? COLLATE NOCASE",
                    (model_code,),
                ).fetchone():
                    raise ApiError("产品编码已存在，请更换", 409)
            else:
                base_sequence = database.execute(
                    "SELECT COUNT(*) AS n FROM product_models WHERE model_code LIKE ?",
                    (f"PRD-{date_code}-%",),
                ).fetchone()["n"] + 1
                for offset in range(10000):
                    model_code = f"PRD-{date_code}-{base_sequence + offset:04d}"
                    if not database.execute(
                        "SELECT 1 FROM product_models WHERE model_code = ? COLLATE NOCASE",
                        (model_code,),
                    ).fetchone():
                        break
                else:
                    raise ApiError("今日产品编码已用尽，请联系管理员", 409)
            simple_cursor = database.execute(
                """
                INSERT INTO product_models(
                    product_family_id, model_code, name, serial_prefix,
                    identity_source, bluetooth_name_prefix,
                    bluetooth_service_uuid, bluetooth_notify_uuid,
                    active, created_at, updated_at, created_by_user_id,
                    attributes_json
                ) VALUES (?, ?, ?, '', 'SCANNER', '', '', '', 1, ?, ?, ?, ?)
                """,
                (
                    family_id, model_code, name, timestamp, timestamp,
                    actor_user_id,
                    json.dumps(
                        product_attributes_with_defaults(
                            attributes,
                            model_code=model_code,
                            name=name,
                            active=True,
                            created_at=timestamp,
                            updated_at=timestamp,
                            created_by=creator_name,
                        ),
                        ensure_ascii=False,
                    ),
                ),
            )
            record_audit_event(
                database,
                "PRODUCT_CREATED",
                "PRODUCT_MODEL",
                model_code,
                payload={"name": name, "componentCount": 0, "simple": True},
                occurred_at=timestamp,
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        row = database.execute(
            "SELECT * FROM product_models WHERE id = ?", (simple_cursor.lastrowid,)
        ).fetchone()
        return success(product_configuration_dict(database, row), 201)
    if len(component_payloads) > MAX_REQUIRED_PARTS:
        raise ApiError(f"一个产品最多添加 {MAX_REQUIRED_PARTS} 行部件")

    components: list[dict[str, Any]] = []
    expanded_count = 0
    for index, item in enumerate(component_payloads, start=1):
        if not isinstance(item, dict):
            raise ApiError(f"第 {index} 个部件格式无效")
        try:
            quantity = int(item.get("quantity") or 1)
        except (TypeError, ValueError) as error:
            raise ApiError(f"第 {index} 个部件数量无效") from error
        if not 1 <= quantity <= MAX_REQUIRED_PARTS:
            raise ApiError(f"第 {index} 个部件数量需为 1-{MAX_REQUIRED_PARTS}")
        expanded_count += quantity
        if expanded_count > MAX_REQUIRED_PARTS:
            raise ApiError(f"一个产品的部件总数最多为 {MAX_REQUIRED_PARTS}")
        inventory_batch_value = item.get("inventoryBatchId")
        if inventory_batch_value not in (None, ""):
            try:
                inventory_batch_id = int(inventory_batch_value)
            except (TypeError, ValueError) as error:
                raise ApiError(f"第 {index} 个部件批次无效") from error
            components.append(
                {"inventoryBatchId": inventory_batch_id, "quantity": quantity}
            )
            continue

        # Backward-compatible import path for older clients. The V2 admin UI
        # uses inventoryBatchId so supplier, part and lot stay normalized.
        part_name = clean_text(
            item.get("name"), f"第 {index} 个部件名称", required=True, max_length=100
        )
        supplier_id_value = item.get("supplierId")
        supplier_id = None
        if supplier_id_value not in (None, ""):
            try:
                supplier_id = int(supplier_id_value)
            except (TypeError, ValueError) as error:
                raise ApiError(f"第 {index} 个部件供应商无效") from error
        supplier_name = clean_text(
            item.get("supplierName"), f"第 {index} 个部件供应商",
            required=supplier_id is None, max_length=100,
        )
        components.append(
            {
                "name": part_name,
                "quantity": quantity,
                "supplierId": supplier_id,
                "supplierName": supplier_name,
                "inventoryBatchId": None,
            }
        )

    database = get_db()
    timestamp = current_app.config["NOW_PROVIDER"]()
    try:
        date_code = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        date_code = datetime.now().astimezone().strftime("%Y%m%d")
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            """
            INSERT OR IGNORE INTO product_families(
                product_code, name, description, active, created_at, updated_at
            ) VALUES ('CUSTOM_PRODUCTS', '自定义产品', '由产品管理统一创建', 1, ?, ?)
            """,
            (timestamp, timestamp),
        )
        family_id = database.execute(
            "SELECT id FROM product_families WHERE product_code = 'CUSTOM_PRODUCTS' COLLATE NOCASE"
        ).fetchone()["id"]
        base_sequence = database.execute(
            "SELECT COUNT(*) AS n FROM product_models WHERE model_code LIKE ?",
            (f"PRD-{date_code}-%",),
        ).fetchone()["n"] + 1
        for offset in range(10000):
            model_code = f"PRD-{date_code}-{base_sequence + offset:04d}"
            if not database.execute(
                "SELECT 1 FROM product_models WHERE model_code = ? COLLATE NOCASE", (model_code,)
            ).fetchone():
                break
        else:
            raise ApiError("今日产品编码已用尽，请联系管理员", 409)
        model_cursor = database.execute(
            """
            INSERT INTO product_models(
                product_family_id, model_code, name, serial_prefix,
                identity_source, bluetooth_name_prefix,
                bluetooth_service_uuid, bluetooth_notify_uuid,
                active, created_at, updated_at, created_by_user_id,
                attributes_json
            ) VALUES (?, ?, ?, '', 'SCANNER', '', '', '', 1, ?, ?, ?, ?)
            """,
            (
                family_id, model_code, name, timestamp, timestamp, actor_user_id,
                json.dumps(
                    product_attributes_with_defaults(
                        attributes,
                        model_code=model_code,
                        name=name,
                        active=True,
                        created_at=timestamp,
                        updated_at=timestamp,
                        created_by=creator_name,
                    ),
                    ensure_ascii=False,
                ),
            ),
        )
        product_model_id = model_cursor.lastrowid
        plan_cursor = database.execute(
            """
            INSERT INTO trace_plans(
                model_code, product_model_id, name, version, status, created_at, activated_at
            ) VALUES (?, ?, ?, 'V1.0', 'ACTIVE', ?, ?)
            """,
            (model_code, product_model_id, f"{name} 部件清单", timestamp, timestamp),
        )
        plan_id = plan_cursor.lastrowid

        slot_position = 1
        for component_index, component in enumerate(components, start=1):
            inventory_batch_id = component.get("inventoryBatchId")
            if inventory_batch_id is not None:
                inventory_batch = database.execute(
                    """
                    SELECT sib.id, sib.part_type_id, sib.active AS batch_active,
                           pt.name AS part_name, pt.active AS part_active,
                           s.active AS supplier_active
                    FROM supplier_inventory_batches sib
                    JOIN part_types pt ON pt.id = sib.part_type_id
                    JOIN suppliers s ON s.id = pt.supplier_id
                    WHERE sib.id = ?
                    """,
                    (inventory_batch_id,),
                ).fetchone()
                if not inventory_batch:
                    raise ApiError(f"第 {component_index} 个部件批次不存在", 404)
                if not (
                    inventory_batch["batch_active"]
                    and inventory_batch["part_active"]
                    and inventory_batch["supplier_active"]
                ):
                    raise ApiError(f"第 {component_index} 个部件批次已停用", 409)
                part_type_id = inventory_batch["part_type_id"]
                part_name = inventory_batch["part_name"]
            else:
                supplier_id = component["supplierId"]
                if supplier_id is not None:
                    supplier = database.execute(
                        "SELECT id, name FROM suppliers WHERE id = ?", (supplier_id,)
                    ).fetchone()
                    if not supplier:
                        raise ApiError(f"第 {component_index} 个部件供应商不存在", 404)
                else:
                    supplier = database.execute(
                        "SELECT id, name FROM suppliers WHERE name = ? COLLATE NOCASE",
                        (component["supplierName"],),
                    ).fetchone()
                    if not supplier:
                        supplier_sequence = database.execute(
                            "SELECT COUNT(*) AS n FROM suppliers WHERE supplier_code LIKE ?",
                            (f"SUP-{date_code}-%",),
                        ).fetchone()["n"] + 1
                        for supplier_offset in range(10000):
                            supplier_code = f"SUP-{date_code}-{supplier_sequence + supplier_offset:04d}"
                            if not database.execute(
                                "SELECT 1 FROM suppliers WHERE supplier_code = ?", (supplier_code,)
                            ).fetchone():
                                break
                        supplier_cursor = database.execute(
                            """
                            INSERT INTO suppliers(
                                supplier_code, name, active, created_at, updated_at
                            ) VALUES (?, ?, 1, ?, ?)
                            """,
                            (supplier_code, component["supplierName"], timestamp, timestamp),
                        )
                        supplier_id = supplier_cursor.lastrowid
                    else:
                        supplier_id = supplier["id"]

                part_code = f"{model_code}-P{component_index:02d}"
                part_cursor = database.execute(
                    """
                    INSERT INTO part_types(
                        part_code, name, category_code, category_name,
                        specification, supplier_id, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', ?, 1, ?, ?)
                    """,
                    (
                        part_code,
                        component["name"],
                        part_code,
                        component["name"],
                        supplier_id,
                        timestamp,
                        timestamp,
                    ),
                )
                part_type_id = part_cursor.lastrowid
                part_name = component["name"]
            for unit_index in range(1, component["quantity"] + 1):
                slot_name = part_name
                if component["quantity"] > 1:
                    slot_name = f"{slot_name} {unit_index}"
                database.execute(
                    """
                    INSERT INTO trace_plan_slots(
                        trace_plan_id, position, slot_name, part_type_id,
                        supplier_inventory_batch_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        plan_id, slot_position, slot_name, part_type_id,
                        inventory_batch_id,
                    ),
                )
                slot_position += 1
        record_audit_event(
            database,
            "PRODUCT_CREATED",
            "PRODUCT_MODEL",
            model_code,
            payload={"name": name, "componentCount": expanded_count},
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute("SELECT * FROM product_models WHERE id = ?", (product_model_id,)).fetchone()
    return success(product_configuration_dict(database, row), 201)


@products_bp.put("/api/products/<int:product_model_id>")
def update_product(product_model_id: int):
    require_operations()
    payload = request.get_json(silent=True) or {}
    database = get_db()
    product = database.execute(
        "SELECT * FROM product_models WHERE id = ?", (product_model_id,)
    ).fetchone()
    if not product:
        raise ApiError("产品不存在", 404)
    actor = current_user()
    actor_role = actor["role"] if actor else ""
    if actor_role != "ADMIN" and product["created_by_user_id"] != current_actor_id():
        raise ApiError("只能维护自己创建的产品", 403)
    name = clean_text(
        payload.get("name", product["name"]), "产品名称", required=True, max_length=100
    )
    active = parse_bool(payload.get("active"), bool(product["active"]))
    timestamp = current_app.config["NOW_PROVIDER"]()
    # Extended profile: merge the submitted columns over the stored ones so a
    # partial save never wipes fields the form did not send. A column sent as
    # null / blank text is an explicit "clear this field" and is removed.
    try:
        stored_attributes = json.loads(product["attributes_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        stored_attributes = {}
    if not isinstance(stored_attributes, dict):
        stored_attributes = {}
    attributes_changed = "attributes" in payload
    submitted_attributes = payload.get("attributes")
    merged_attributes = {
        **stored_attributes,
        **clean_product_attributes(submitted_attributes),
    }
    for column in cleared_product_attribute_columns(submitted_attributes):
        merged_attributes.pop(column, None)
    merged_attributes = clean_product_attributes(merged_attributes)
    attributes_json = json.dumps(
        product_attributes_with_defaults(
            merged_attributes,
            model_code=product["model_code"],
            name=name,
            active=bool(active),
            created_at=stored_attributes.get("创建时间") or product["created_at"],
            updated_at=timestamp,
            created_by=stored_attributes.get("创建人") or current_actor_name("系统管理员"),
        ),
        ensure_ascii=False,
    )
    # Only admins manage the manufacturing BOM; operations edits are limited
    # to name/active/profile on their own products.
    raw_components = payload.get("components") if actor_role == "ADMIN" else None
    components: list[dict[str, int]] | None = None
    if raw_components is not None:
        # An empty list is allowed and means "no BOM": the product becomes a
        # complete, indivisible item (its active plan is archived and no new
        # slots are created). A non-list payload is still rejected.
        if not isinstance(raw_components, list):
            raise ApiError("产品部件格式无效")
        components = []
        expanded_count = 0
        for index, item in enumerate(raw_components, 1):
            if not isinstance(item, dict):
                raise ApiError(f"第 {index} 个部件格式无效")
            try:
                inventory_batch_id = int(item.get("inventoryBatchId"))
                quantity = int(item.get("quantity") or 1)
            except (TypeError, ValueError) as error:
                raise ApiError(f"第 {index} 个部件批次或数量无效") from error
            if not 1 <= quantity <= MAX_REQUIRED_PARTS:
                raise ApiError(f"第 {index} 个部件数量需为 1-{MAX_REQUIRED_PARTS}")
            expanded_count += quantity
            if expanded_count > MAX_REQUIRED_PARTS:
                raise ApiError(f"一个产品的部件总数最多为 {MAX_REQUIRED_PARTS}")
            components.append(
                {"inventoryBatchId": inventory_batch_id, "quantity": quantity}
            )

    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            "UPDATE product_models SET name = ?, active = ?, updated_at = ?, "
            "attributes_json = ? WHERE id = ?",
            (name, int(active), timestamp, attributes_json, product_model_id),
        )
        if components is not None:
            plan_number = database.execute(
                "SELECT COUNT(*) AS n FROM trace_plans WHERE product_model_id = ?",
                (product_model_id,),
            ).fetchone()["n"] + 1
            database.execute(
                "UPDATE trace_plans SET status = 'ARCHIVED' "
                "WHERE product_model_id = ? AND status = 'ACTIVE'",
                (product_model_id,),
            )
        if components:
            plan_cursor = database.execute(
                """
                INSERT INTO trace_plans(
                    model_code, product_model_id, name, version,
                    status, created_at, activated_at
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    product["model_code"], product_model_id, f"{name} 部件清单",
                    f"V{plan_number}.0", timestamp, timestamp,
                ),
            )
            position = 1
            for component_index, component in enumerate(components, 1):
                batch = database.execute(
                    """
                    SELECT sib.id, sib.part_type_id, sib.active AS batch_active,
                           pt.name AS part_name, pt.active AS part_active,
                           s.active AS supplier_active
                    FROM supplier_inventory_batches sib
                    JOIN part_types pt ON pt.id = sib.part_type_id
                    JOIN suppliers s ON s.id = pt.supplier_id
                    WHERE sib.id = ?
                    """,
                    (component["inventoryBatchId"],),
                ).fetchone()
                if not batch:
                    raise ApiError(f"第 {component_index} 个部件批次不存在", 404)
                if not (batch["batch_active"] and batch["part_active"] and batch["supplier_active"]):
                    raise ApiError(f"第 {component_index} 个部件批次已停用", 409)
                for unit_index in range(1, component["quantity"] + 1):
                    slot_name = batch["part_name"]
                    if component["quantity"] > 1:
                        slot_name = f"{slot_name} {unit_index}"
                    database.execute(
                        """
                        INSERT INTO trace_plan_slots(
                            trace_plan_id, position, slot_name, part_type_id,
                            supplier_inventory_batch_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            plan_cursor.lastrowid, position, slot_name,
                            batch["part_type_id"], batch["id"],
                        ),
                    )
                    position += 1
        record_audit_event(
            database,
            "PRODUCT_UPDATED",
            "PRODUCT_MODEL",
            product["model_code"],
            payload={
                "name": name,
                "active": active,
                "componentsChanged": components is not None,
                "componentCount": sum(item["quantity"] for item in components or []),
                "attributesChanged": attributes_changed,
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute(
        "SELECT * FROM product_models WHERE id = ?", (product_model_id,)
    ).fetchone()
    return success(product_configuration_dict(database, row))
