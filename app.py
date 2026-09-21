from __future__ import annotations

import os
import csv
import json
import re
import secrets
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from traceability.codes import (
    batch_identification_code,
    machine_identification_code,
    make_qr_svg,
    new_batch_code,
    new_event_id,
    new_label_batch_code,
    new_part_identification_code,
    new_trace_number,
    normalize_entity_code,
    normalize_sn,
    normalize_station_id,
    parse_batch_payload,
)
from traceability.ble_collector import (
    BluetoothCollectionError,
    discover_v2_devices,
    is_bluetooth_runtime_available,
    read_v2_identity,
)
from traceability.db import get_db, initialize_database
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    current_operator_id,
    current_user,
    initialize_auth,
    require_admin,
    require_admin_or_warehouse,
    require_capability,
    require_operations,
    require_product_model_access,
    require_supplier_access,
    require_warehouse,
)
from traceability.capabilities import Capability
from traceability.lingxing import (
    DEFAULT_API_BASE_URL,
    DEFAULT_INVENTORY_RECEIVE_PATH,
    DEFAULT_PURCHASE_ORDER_PATH,
    DEFAULT_RECEIPT_LIST_PATH,
    DEFAULT_REFRESH_TOKEN_PATH,
    DEFAULT_TOKEN_PATH,
    LingxingError,
    LingxingIntegrationService,
    LingxingTokenCache,
    RetryConfig,
    load_credentials,
    mask_secret,
)
from traceability.xlsx_export import build_traceability_xlsx


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "traceability.db"
# Caps the expanded number of component slots per product (sum of per-component
# quantities) accepted by ``POST /api/products``.
MAX_REQUIRED_PARTS = 20
# Fixed number of components expected by the legacy per-unit (逐台) generic scan
# flow when a product has no BOM/expected slots. Formerly configurable via the
# ``required_part_count`` system setting, which has been removed.
DEFAULT_GENERIC_PART_COUNT = 2
SESSION_TIMEOUT_HOURS = 8
BLUETOOTH_OPERATION_LOCK = threading.Lock()
DEFAULT_SECRET_KEY = "pts-local-development-key-change-before-server-deployment"
DEFAULT_BOOTSTRAP_PASSWORD = "Admin@12345"
EXAMPLE_SECRET_KEY = "replace-with-a-random-string-of-at-least-32-characters"
EXAMPLE_BOOTSTRAP_PASSWORD = "replace-with-a-strong-initial-password"
LINGXING_TOKEN_CACHE = LingxingTokenCache()
PURCHASE_ORDER_EXPORT_COLUMNS = [
    "标识号", "采购单号", "供应商", "联系人", "采购方", "联系方式", "结算方式",
    "预付比例", "结算账期", "结算描述", "支付方式", "含税", "费用分配方式",
    "采购币种", "当前汇率", "运费", "运费币种", "其他费用", "其他费用币种",
    "采购员", "质检类型", "单据备注", "颜色", "材质", "内含配件", "包装要求",
    "特殊要求", "HS海关编码", "交货周期（天数）", "采购仓库", "计划编号", "SKU",
    "店铺", "FNSKU", "是否赠品", "单箱数量", "箱数", "实际采购量", "含税单价",
    "税率", "预计到货时间", "产品备注", "更新报价", "内含配件(产品)",
    "包装要求(产品)", "特殊要求（规避专利）", "HS海关编码(产品)",
    "交货周期（天数）(产品)",
]
PURCHASE_ORDER_EXPORT_COLUMN_SET = set(PURCHASE_ORDER_EXPORT_COLUMNS)
# Extended product profile (product management). Stored per product in
# ``product_models.attributes_json`` keyed by column name so the catalog matches
# the external product template without one SQL column per attribute.
PRODUCT_ATTRIBUTE_COLUMNS = [
    "SKU", "品名", "主图", "SPU", "款名", "属性", "创建时间", "创建人", "更新时间",
    "产品类型", "包含单品", "单位加工费", "加工备注", "关联单品成本", "识别码",
    "状态", "型号", "单位", "产品材质", "一级分类", "二级分类", "三级分类", "品牌",
    "产品标签", "开发人", "产品负责人", "产品描述", "产品描述(纯文本)", "图片链接",
    "采购员", "采购交期", "采购成本(CNY)", "采购备注", "单品规格长", "单品规格宽",
    "单品规格高", "单品规格单位", "单品净重", "单品净重单位", "单品毛重",
    "单品毛重单位", "包装规格长", "包装规格宽", "包装规格高", "包装规格单位",
    "外箱规格长", "外箱规格宽", "外箱规格高", "外箱规格单位", "单箱重量",
    "单箱重量单位", "单箱数量(pcs)", "供应商名称", "币种", "含税", "税率",
    "最小采购量", "单价", "含税单价", "交期", "采购链接", "报价备注",
    "默认质检方式", "质检模板", "中文报关名", "英文报关名", "中文材质", "英文材质",
    "中文用途", "英文用途", "品牌类型", "出口享惠情况", "内部编码", "产品属性",
    "报关单价", "报关单价币种", "报关HSCODE", "报关型号", "原产国(地区)",
    "境内货源地", "报关单位", "其他申报要素", "征免", "生产销售企业名称",
    "生产销售企业代码", "清关型号", "配货备注", "织造方式", "默认清关HSCODE",
    "默认清关单价", "默认清关单价币种", "默认清关税率", "默认清关备注",
    "全部国家头程费用(含税)", "全部国家头程费用币种", "巴西发票默认NCM",
    "巴西发票默认单位", "巴西发票默认原产地",
]
PRODUCT_ATTRIBUTE_COLUMN_SET = set(PRODUCT_ATTRIBUTE_COLUMNS)
# Profile columns holding an uploaded picture rather than typed text. The stored
# value is the local URL returned by the upload endpoint.
PRODUCT_IMAGE_COLUMNS = ["主图"]
PRODUCT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
# Accepted picture formats, keyed by the file signature so a renamed executable
# can never be stored (the browser-supplied name and type are not trusted).
PRODUCT_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
PRODUCT_IMAGE_EXTENSIONS = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
PRODUCT_IMAGE_NAME_PATTERN = re.compile(r"^[0-9a-f]{8,}-[0-9a-f]{16,}\.(?:png|jpg|gif|webp)$")


def detect_product_image_type(data: bytes) -> tuple[str, str]:
    """Return ``(extension, mimetype)`` for supported image bytes.

    Detection is by file signature, so the stored file really is a picture
    regardless of the uploaded filename or the declared content type.
    """
    for signature, extension, mimetype in PRODUCT_IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, mimetype
    # WEBP: "RIFF" .... "WEBP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ApiError("仅支持 PNG、JPG、GIF 或 WEBP 图片")
# Filled in by the system from the product record itself, so the form never asks
# for them and a client cannot desynchronize them from the real product.
PRODUCT_ATTRIBUTE_DERIVED_COLUMNS = {
    "SKU", "品名", "创建时间", "创建人", "更新时间", "状态", "识别码",
}
# Columns whose values are numeric so the exported cell keeps a numeric type /
# format instead of being written as text.
PURCHASE_ORDER_NUMERIC_COLUMNS = {
    "预付比例", "结算账期", "当前汇率", "运费", "其他费用", "单箱数量", "箱数",
    "实际采购量", "含税单价", "税率",
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# Requirement 7.3: treadmill (走步机) products moved to batch-level traceability,
# so their legacy per-unit main-code + per-component assembly entry endpoints are
# retired. Direct calls to those entry points are rejected with this message and
# an HTTP 409, without creating any per-unit traceability record. Non-treadmill
# products keep their previous behavior unchanged (Requirement 7.5), and historical
# ``machines`` / ``trace_records`` / ``product_code_sets`` rows stay read-only
# queryable (Requirement 7.4).
TREADMILL_FAMILY_CODE = "TREADMILL"
LEGACY_ENTRY_DISABLED_MESSAGE = "走步机产品已改为按批次管理，逐台/逐部件录入入口已停用"


def is_treadmill_product_model(
    database: sqlite3.Connection, product_model_id: int | None
) -> bool:
    """Return True when ``product_model_id`` belongs to the treadmill family.

    The treadmill product family (``product_families.product_code = 'TREADMILL'``)
    is the one whose per-unit / per-component entry has been retired in favor of
    batch traceability (Requirement 7.3). Any other family (bed frames, custom
    products, legacy imports, ...) is left untouched (Requirement 7.5). A missing
    or unknown product model resolves to ``False`` so callers fall through to
    their normal not-found handling instead of masking it as a disabled entry.
    """
    if product_model_id is None:
        return False
    row = database.execute(
        """
        SELECT pf.product_code AS family_code
        FROM product_models pm
        JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE pm.id = ?
        """,
        (product_model_id,),
    ).fetchone()
    if row is None:
        return False
    return (row["family_code"] or "").strip().upper() == TREADMILL_FAMILY_CODE


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_text(value: object, label: str, *, required: bool = False, max_length: int = 100) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ApiError(f"请输入{label}")
    if len(text) > max_length:
        raise ApiError(f"{label}不能超过 {max_length} 个字符")
    return text


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def coerce_export_number(value: object) -> int | float | None:
    """Convert a numeric-looking string into int/float for numeric export cells.

    Returns ``None`` when the value is not a plain number so callers can keep
    the original text.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return float(text)
    except ValueError:
        return None


def clean_product_attributes(raw_attributes: object) -> dict[str, Any]:
    """Normalize the extended product profile entered in product management.

    Values are keyed by the template column name; unknown columns and the
    system-derived ones are dropped so the stored payload always maps onto the
    template and can never contradict the product record itself.
    """
    attributes: dict[str, Any] = {}
    if raw_attributes is None:
        return attributes
    if not isinstance(raw_attributes, dict):
        raise ApiError("产品资料字段格式无效")
    for key, value in raw_attributes.items():
        column = str(key).strip()
        if column not in PRODUCT_ATTRIBUTE_COLUMN_SET:
            continue
        if column in PRODUCT_ATTRIBUTE_DERIVED_COLUMNS:
            continue
        if value is None:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            attributes[column] = value
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) > 500:
            raise ApiError(f"字段“{column}”内容不能超过 500 个字符")
        attributes[column] = text
    return attributes


def product_attributes_with_defaults(
    attributes: dict[str, Any],
    *,
    model_code: str,
    name: str,
    active: bool,
    created_at: str,
    updated_at: str,
    created_by: str,
) -> dict[str, Any]:
    """Stamp the system-managed columns onto a product's stored profile.

    ``创建人`` defaults to the account saving the product (operations register
    their own products), and SKU / 品名 / 状态 / timestamps always mirror the
    product record so the profile stays truthful.
    """
    derived = dict(attributes)
    derived["SKU"] = model_code
    derived["识别码"] = model_code
    derived["品名"] = name
    derived["状态"] = "启用" if active else "停用"
    derived["创建时间"] = created_at
    derived["更新时间"] = updated_at
    derived["创建人"] = created_by
    return derived


def safe_archive_name(value: object, fallback: str = "item") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    name = name.strip(". ")
    return name[:100] or fallback


# Lingxing business write endpoints are configured (route path or full URL) in
# settings so operations can enable the push features without redeploying.
LINGXING_ENDPOINT_SETTING_KEYS = {
    "purchase_order": "lingxing.endpoint.purchase_order",
    "inbound_receipt": "lingxing.endpoint.inbound_receipt",
    "inventory_sync": "lingxing.endpoint.inventory_sync",
}


def load_lingxing_endpoint_overrides(database: sqlite3.Connection) -> dict[str, str]:
    """Return the non-empty Lingxing endpoint overrides stored in app_settings."""
    try:
        rows = database.execute(
            "SELECT setting_key, setting_value FROM app_settings WHERE setting_key IN (?, ?, ?)",
            tuple(LINGXING_ENDPOINT_SETTING_KEYS.values()),
        ).fetchall()
    except sqlite3.Error:
        return {}
    stored = {str(row["setting_key"]): str(row["setting_value"] or "").strip() for row in rows}
    overrides: dict[str, str] = {}
    for operation, key in LINGXING_ENDPOINT_SETTING_KEYS.items():
        value = stored.get(key, "")
        if value:
            overrides[operation] = value
    return overrides


def clean_lingxing_endpoint(value: object, label: str) -> str:
    """Validate a Lingxing endpoint value: empty, a ``/path`` or an http(s) URL."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 300:
        raise ApiError(f"{label}不能超过 300 个字符")
    if not re.fullmatch(r"(?:https?://[^\s]+|/[^\s]*)", text):
        raise ApiError(f"{label}需为以 / 开头的路径或 http(s) 链接")
    return text


def clean_ble_uuid(value: object, label: str, *, required: bool = False) -> str:
    uuid = clean_text(value, label, required=required, max_length=36).lower()
    if uuid and not re.fullmatch(
        r"(?:[0-9a-f]{4}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        uuid,
    ):
        raise ApiError(f"{label}格式无效")
    return uuid


def require_quality_release(database: sqlite3.Connection) -> bool:
    row = database.execute(
        "SELECT setting_value FROM system_settings WHERE setting_key = 'require_quality_release'"
    ).fetchone()
    return parse_bool(row["setting_value"] if row else "0")


def record_audit_event(
    database: sqlite3.Connection,
    event_type: str,
    object_type: str,
    object_code: str,
    *,
    related_object_code: str = "",
    station_id: str = "",
    station_name: str = "",
    operator_name: str = "",
    reason: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> None:
    actor_user_id = current_actor_id()
    if actor_user_id is not None:
        operator_name = current_actor_name(operator_name)
    for attempt in range(8):
        try:
            database.execute(
                """
                INSERT INTO audit_events(
                    event_id, event_type, object_type, object_code, related_object_code,
                    station_id, station_name, operator_name, actor_user_id,
                    reason, payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_event_id(),
                    event_type,
                    object_type,
                    object_code,
                    related_object_code,
                    station_id,
                    station_name,
                    operator_name,
                    actor_user_id,
                    reason,
                    json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                    occurred_at or now_iso(),
                ),
            )
            return
        except sqlite3.IntegrityError as error:
            if "audit_events.event_id" not in str(error) or attempt == 7:
                raise


def audit_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": row["id"],
        "eventId": row["event_id"],
        "eventType": row["event_type"],
        "objectType": row["object_type"],
        "objectCode": row["object_code"],
        "relatedObjectCode": row["related_object_code"],
        "stationId": row["station_id"],
        "stationName": row["station_name"],
        "operatorName": row["operator_name"],
        "actorUserId": row["actor_user_id"] if "actor_user_id" in row.keys() else None,
        "actorUsername": row["actor_username"] if "actor_username" in row.keys() else "",
        "actorDisplayName": row["actor_display_name"] if "actor_display_name" in row.keys() else "",
        "reason": row["reason"],
        "payload": payload,
        "occurredAt": row["occurred_at"],
    }


# Shared SELECT for a trace plan's slots joined to part / supplier / inventory
# data. Reused by the single-plan lookup and the batched products-list path so
# both produce identical slot rows.
_TRACE_PLAN_SLOT_SELECT = """
        SELECT tps.*, pt.part_code, pt.name AS part_name, pt.specification,
               pt.minimum_stock,
               pt.category_code, pt.category_name,
               s.id AS supplier_id, s.supplier_code, s.name AS supplier_name,
               sib.batch_no AS inventory_batch_no,
               sib.quantity_received AS inventory_quantity_received,
               sib.quantity_available AS inventory_quantity_available,
               sib.production_date AS inventory_production_date,
               sib.received_date AS inventory_received_date,
               sib.active AS inventory_batch_active
        FROM trace_plan_slots tps
        JOIN part_types pt ON pt.id = tps.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN supplier_inventory_batches sib ON sib.id = tps.supplier_inventory_batch_id
"""


def _trace_plan_slot_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "position": row["position"],
        "slotName": row["slot_name"],
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"],
        "partName": row["part_name"],
        "categoryCode": row["category_code"],
        "categoryName": row["category_name"],
        "specification": row["specification"],
        "minimumStock": row["minimum_stock"],
        "supplierId": row["supplier_id"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "inventoryBatchId": row["supplier_inventory_batch_id"],
        "inventoryBatchNo": row["inventory_batch_no"],
        "inventoryQuantityReceived": row["inventory_quantity_received"],
        "inventoryQuantityAvailable": row["inventory_quantity_available"],
        "inventoryProductionDate": row["inventory_production_date"],
        "inventoryReceivedDate": row["inventory_received_date"],
        "inventoryBatchActive": (
            bool(row["inventory_batch_active"])
            if row["inventory_batch_active"] is not None else None
        ),
    }


def trace_plan_slots(database: sqlite3.Connection, plan_id: int | None) -> list[dict[str, Any]]:
    if not plan_id:
        return []
    rows = database.execute(
        _TRACE_PLAN_SLOT_SELECT + " WHERE tps.trace_plan_id = ? ORDER BY tps.position",
        (plan_id,),
    ).fetchall()
    return [_trace_plan_slot_dict(row) for row in rows]


def trace_plan_dict(database: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    slots = trace_plan_slots(database, row["id"])
    keys = set(row.keys())
    return {
        "id": row["id"],
        "modelCode": row["model_code"],
        "productModelId": row["product_model_id"] if "product_model_id" in keys else None,
        "productModelName": row["product_model_name"] if "product_model_name" in keys else None,
        "productFamilyId": row["product_family_id"] if "product_family_id" in keys else None,
        "productFamilyCode": row["product_family_code"] if "product_family_code" in keys else None,
        "productFamilyName": row["product_family_name"] if "product_family_name" in keys else None,
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "activatedAt": row["activated_at"],
        "usedMachineCount": row["used_machine_count"] if "used_machine_count" in keys else 0,
        "slots": slots,
    }


def get_plan_summary(database: sqlite3.Connection, plan_id: int | None) -> dict[str, Any] | None:
    if not plan_id:
        return None
    row = database.execute(
        """
        SELECT tp.*, pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name
        FROM trace_plans tp
        LEFT JOIN product_models pm ON pm.id = tp.product_model_id
        LEFT JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE tp.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if not row:
        return None
    return trace_plan_dict(database, row)


def supplier_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "supplierCode": row["supplier_code"],
        "name": row["name"],
        "contact": row["contact"],
        "phone": row["phone"],
        "active": bool(row["active"]) if "active" in keys else True,
        "partCount": row["part_count"] if "part_count" in keys else 0,
        "batchCount": row["batch_count"] if "batch_count" in keys else 0,
        "quantityAvailable": row["quantity_available"] if "quantity_available" in keys else 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"] if "updated_at" in keys else row["created_at"],
    }


def part_type_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    quantity_available = row["quantity_available"] if "quantity_available" in keys else 0
    minimum_stock = row["minimum_stock"] if "minimum_stock" in keys else 0
    if quantity_available <= 0:
        stock_status = "OUT"
    elif minimum_stock > 0 and quantity_available <= minimum_stock:
        stock_status = "LOW"
    else:
        stock_status = "NORMAL"
    return {
        "id": row["id"],
        "partCode": row["part_code"],
        "name": row["name"],
        "categoryCode": row["category_code"],
        "categoryName": row["category_name"],
        "specification": row["specification"],
        "minimumStock": minimum_stock,
        "supplierId": row["supplier_id"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "active": bool(row["active"]),
        "batchCount": row["batch_count"] if "batch_count" in keys else 0,
        "quantityAvailable": quantity_available,
        "stockStatus": stock_status,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"] if "updated_at" in keys else row["created_at"],
    }


def supplier_inventory_batch_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    received = row["quantity_received"]
    available = row["quantity_available"]
    return {
        "id": row["id"],
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"] if "part_code" in keys else None,
        "partName": row["part_name"] if "part_name" in keys else None,
        "specification": row["specification"] if "specification" in keys else "",
        "supplierId": row["supplier_id"] if "supplier_id" in keys else None,
        "supplierCode": row["supplier_code"] if "supplier_code" in keys else None,
        "supplierName": row["supplier_name"] if "supplier_name" in keys else None,
        "batchNo": row["batch_no"],
        "quantityReceived": received,
        "quantityAvailable": available,
        "quantityConsumed": received - available,
        "productionDate": row["production_date"],
        "receivedDate": row["received_date"],
        "remarks": row["remarks"],
        "active": bool(row["active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def product_family_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "productCode": row["product_code"],
        "name": row["name"],
        "description": row["description"],
        "active": bool(row["active"]),
        "modelCount": row["model_count"] if "model_count" in keys else 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def product_model_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "productFamilyId": row["product_family_id"],
        "productFamilyCode": row["product_family_code"] if "product_family_code" in keys else None,
        "productFamilyName": row["product_family_name"] if "product_family_name" in keys else None,
        "modelCode": row["model_code"],
        "name": row["name"],
        "serialPrefix": row["serial_prefix"],
        "identitySource": row["identity_source"],
        "bluetoothNamePrefix": row["bluetooth_name_prefix"],
        "bluetoothServiceUuid": row["bluetooth_service_uuid"] if "bluetooth_service_uuid" in keys else "",
        "bluetoothNotifyUuid": row["bluetooth_notify_uuid"] if "bluetooth_notify_uuid" in keys else "",
        "active": bool(row["active"]),
        "finishedProductCount": row["finished_product_count"] if "finished_product_count" in keys else 0,
        "tracePlanCount": row["trace_plan_count"] if "trace_plan_count" in keys else 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _assemble_product_configuration(
    row: sqlite3.Row,
    *,
    plan: sqlite3.Row | None,
    components: list[dict[str, Any]],
    generated_count: int,
    generation_batch_count: int,
    created_by: str,
) -> dict[str, Any]:
    """Build the product configuration payload from already-fetched pieces.

    Kept free of database access so both the single-product serializer and the
    batched list path share one shape.
    """
    requirements: dict[int, dict[str, int]] = {}
    for component in components:
        batch_id = component["inventoryBatchId"]
        if batch_id is None:
            continue
        requirement = requirements.setdefault(
            batch_id,
            {"required": 0, "available": component["inventoryQuantityAvailable"] or 0},
        )
        requirement["required"] += 1
    available_units = (
        min(item["available"] // item["required"] for item in requirements.values())
        if requirements else None
    )
    created_by_user_id = (
        row["created_by_user_id"] if "created_by_user_id" in row.keys() else None
    )
    try:
        attributes = (
            json.loads(row["attributes_json"] or "{}")
            if "attributes_json" in row.keys() else {}
        )
    except (json.JSONDecodeError, TypeError):
        attributes = {}
    if not isinstance(attributes, dict):
        attributes = {}
    return {
        "id": row["id"],
        "productCode": row["model_code"],
        "name": row["name"],
        "active": bool(row["active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"] if "updated_at" in row.keys() else None,
        "attributes": attributes,
        "createdByUserId": created_by_user_id,
        "createdBy": created_by,
        "tracePlanId": plan["id"] if plan else None,
        "components": components,
        "componentCount": len(components),
        "generatedCount": generated_count,
        "generationBatchCount": generation_batch_count,
        "availableUnits": available_units,
    }


def product_configuration_dict(
    database: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    plan = database.execute(
        """
        SELECT * FROM trace_plans
        WHERE product_model_id = ? AND status = 'ACTIVE'
        ORDER BY activated_at DESC, id DESC LIMIT 1
        """,
        (row["id"],),
    ).fetchone()
    components = trace_plan_slots(database, plan["id"]) if plan else []
    generated_count = database.execute(
        "SELECT COUNT(*) AS n FROM product_code_sets WHERE product_model_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    generation_batch_count = database.execute(
        "SELECT COUNT(*) AS n FROM product_code_batches WHERE product_model_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    created_by = ""
    created_by_user_id = (
        row["created_by_user_id"] if "created_by_user_id" in row.keys() else None
    )
    if created_by_user_id is not None:
        creator = database.execute(
            "SELECT display_name, username FROM users WHERE id = ?",
            (created_by_user_id,),
        ).fetchone()
        if creator:
            created_by = creator["display_name"] or creator["username"] or ""
    return _assemble_product_configuration(
        row,
        plan=plan,
        components=components,
        generated_count=generated_count,
        generation_batch_count=generation_batch_count,
        created_by=created_by,
    )


def list_product_configurations(
    database: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    """Serialize many products with a bounded number of queries.

    Resolves the active trace plan, its slots, the two generation counts and the
    creator names for every product in a handful of aggregate queries instead of
    ~5-6 per product (avoids the N+1 on ``GET /api/products``). Produces exactly
    the same payload as ``product_configuration_dict`` per row.
    """
    if not rows:
        return []
    product_ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in product_ids)

    # Active trace plan per product (at most one ACTIVE; keep the latest if the
    # data ever holds several, matching the single-row ordering).
    plan_by_product: dict[int, sqlite3.Row] = {}
    for plan in database.execute(
        f"""
        SELECT * FROM trace_plans
        WHERE status = 'ACTIVE' AND product_model_id IN ({placeholders})
        ORDER BY product_model_id, activated_at DESC, id DESC
        """,
        product_ids,
    ).fetchall():
        plan_by_product.setdefault(plan["product_model_id"], plan)

    # Slots for every active plan in one query, grouped by plan.
    slots_by_plan: dict[int, list[dict[str, Any]]] = defaultdict(list)
    plan_ids = [plan["id"] for plan in plan_by_product.values()]
    if plan_ids:
        slot_placeholders = ",".join("?" for _ in plan_ids)
        for slot_row in database.execute(
            _TRACE_PLAN_SLOT_SELECT
            + f" WHERE tps.trace_plan_id IN ({slot_placeholders})"
            + " ORDER BY tps.trace_plan_id, tps.position",
            plan_ids,
        ).fetchall():
            slots_by_plan[slot_row["trace_plan_id"]].append(_trace_plan_slot_dict(slot_row))

    # Generation counts in one aggregate query each.
    code_set_counts = {
        r["product_model_id"]: r["n"]
        for r in database.execute(
            f"SELECT product_model_id, COUNT(*) AS n FROM product_code_sets "
            f"WHERE product_model_id IN ({placeholders}) GROUP BY product_model_id",
            product_ids,
        ).fetchall()
    }
    code_batch_counts = {
        r["product_model_id"]: r["n"]
        for r in database.execute(
            f"SELECT product_model_id, COUNT(*) AS n FROM product_code_batches "
            f"WHERE product_model_id IN ({placeholders}) GROUP BY product_model_id",
            product_ids,
        ).fetchall()
    }

    # Creator display names in one query.
    creator_names: dict[int, str] = {}
    creator_ids = {
        row["created_by_user_id"]
        for row in rows
        if "created_by_user_id" in row.keys() and row["created_by_user_id"] is not None
    }
    if creator_ids:
        creator_placeholders = ",".join("?" for _ in creator_ids)
        for creator in database.execute(
            f"SELECT id, display_name, username FROM users WHERE id IN ({creator_placeholders})",
            list(creator_ids),
        ).fetchall():
            creator_names[creator["id"]] = creator["display_name"] or creator["username"] or ""

    payload = []
    for row in rows:
        plan = plan_by_product.get(row["id"])
        components = slots_by_plan.get(plan["id"], []) if plan else []
        created_by_user_id = (
            row["created_by_user_id"] if "created_by_user_id" in row.keys() else None
        )
        payload.append(
            _assemble_product_configuration(
                row,
                plan=plan,
                components=components,
                generated_count=code_set_counts.get(row["id"], 0),
                generation_batch_count=code_batch_counts.get(row["id"], 0),
                created_by=creator_names.get(created_by_user_id, ""),
            )
        )
    return payload


def product_code_set_dict(database: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    part_rows = database.execute(
        """
        SELECT pcsp.position, pcsp.slot_name, pl.id AS part_label_id,
               pl.identification_code, pt.name AS part_name,
               s.name AS supplier_name
        FROM product_code_set_parts pcsp
        JOIN part_labels pl ON pl.id = pcsp.part_label_id
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        WHERE pcsp.product_code_set_id = ?
        ORDER BY pcsp.position
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "generationBatchId": row["generation_batch_id"] if "generation_batch_id" in row.keys() else None,
        "productModelId": row["product_model_id"],
        "productName": row["product_name"],
        "setCode": row["set_code"],
        "prefix": row["prefix"],
        "dateCode": row["date_code"],
        "sequence": row["daily_sequence"],
        "generatedAt": row["generated_at"],
        "machine": {
            "id": row["machine_id"],
            "sn": row["machine_sn"],
            "identificationCode": row["machine_code"],
            "qrUrl": f"/api/machines/{row['machine_id']}/qr",
        },
        "parts": [
            {
                "position": item["position"],
                "slotName": item["slot_name"],
                "partLabelId": item["part_label_id"],
                "partName": item["part_name"],
                "supplierName": item["supplier_name"],
                "identificationCode": item["identification_code"],
                "qrUrl": f"/api/part-labels/{item['part_label_id']}/qr",
            }
            for item in part_rows
        ],
        "downloadUrl": f"/api/product-code-sets/{row['id']}/qrcodes.zip",
    }


def product_code_batch_dict(row: sqlite3.Row, *, include_sets: bool = False) -> dict[str, Any]:
    keys = set(row.keys())
    data = {
        "id": row["id"],
        "batchCode": row["batch_code"],
        "productModelId": row["product_model_id"],
        "productName": row["product_name"] if "product_name" in keys else None,
        "tracePlanId": row["trace_plan_id"],
        "prefix": row["prefix"],
        "dateCode": row["date_code"],
        "quantity": row["quantity"],
        "startSequence": row["start_sequence"],
        "endSequence": row["end_sequence"],
        "generatedBy": row["generated_by"] if "generated_by" in keys else "",
        "generatedAt": row["generated_at"],
        "downloadUrl": f"/api/product-code-batches/{row['id']}/qrcodes.zip",
    }
    if include_sets and "sets" in keys:
        data["sets"] = row["sets"]
    return data


def machine_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "sn": row["sn"],
        "model": row["model"],
        "productModelId": row["product_model_id"] if "product_model_id" in keys else None,
        "productModelName": row["product_model_name"] if "product_model_name" in keys else None,
        "productFamilyId": row["product_family_id"] if "product_family_id" in keys else None,
        "productFamilyCode": row["product_family_code"] if "product_family_code" in keys else None,
        "productFamilyName": row["product_family_name"] if "product_family_name" in keys else None,
        "productionDate": row["production_date"],
        "tracePlanId": row["trace_plan_id"] if "trace_plan_id" in keys else None,
        "tracePlanVersion": row["trace_plan_version"] if "trace_plan_version" in keys else None,
        "tracePlanName": row["trace_plan_name"] if "trace_plan_name" in keys else None,
        "identificationCode": row["identification_code"],
        "qrUrl": f"/api/machines/{row['id']}/qr",
        "createdAt": row["created_at"],
        "traced": bool(row["traced"]) if "traced" in keys else False,
        "reservedStation": row["reserved_station"] if "reserved_station" in keys else None,
    }


def part_label_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "identificationCode": row["identification_code"],
        "qrUrl": f"/api/part-labels/{row['id']}/qr",
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"],
        "partName": row["part_name"],
        "categoryCode": row["category_code"] if "category_code" in keys else row["part_code"],
        "categoryName": row["category_name"] if "category_name" in keys else row["part_name"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "labelBatchId": row["label_batch_id"] if "label_batch_id" in keys else None,
        "batchCode": row["batch_code"] if "batch_code" in keys else None,
        "batchQuantity": row["batch_quantity"] if "batch_quantity" in keys else None,
        "batchGeneratedAt": row["batch_generated_at"] if "batch_generated_at" in keys else None,
        "batchGeneratedBy": row["batch_generated_by"] if "batch_generated_by" in keys else None,
        "lotNo": row["lot_no"],
        "supplierBatchNo": row["supplier_batch_no"],
        "sourceSerialNo": row["source_serial_no"] if "source_serial_no" in keys else "",
        "productionDate": row["production_date"] if "production_date" in keys else "",
        "inspectionStatus": row["inspection_status"] if "inspection_status" in keys else "PENDING",
        "remarks": row["remarks"] if "remarks" in keys else "",
        "enteredBy": row["entered_by"] if "entered_by" in keys else "",
        "enteredAt": row["entered_at"] if "entered_at" in keys else None,
        "updatedAt": row["updated_at"] if "updated_at" in keys else None,
        "dataEntered": bool(row["entered_at"]) if "entered_at" in keys else False,
        "createdAt": row["created_at"],
        "used": bool(row["used"]) if "used" in keys else False,
        "reservedStation": row["reserved_station"] if "reserved_station" in keys else None,
    }


def part_label_batch_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "batchCode": row["batch_code"],
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"],
        "partName": row["part_name"],
        "categoryCode": row["category_code"] if "category_code" in keys else row["part_code"],
        "categoryName": row["category_name"] if "category_name" in keys else row["part_name"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "lotNo": row["lot_no"],
        "supplierBatchNo": row["supplier_batch_no"],
        "quantity": row["quantity"],
        "productionDate": row["production_date"],
        "generatedBy": row["generated_by"],
        "generatedAt": row["generated_at"],
        "generatedCount": row["generated_count"] if "generated_count" in keys else row["quantity"],
        "dataEnteredCount": row["data_entered_count"] if "data_entered_count" in keys else 0,
    }


def ensure_station_access(database: sqlite3.Connection, station_id: str) -> None:
    actor_user_id = current_actor_id()
    if actor_user_id is None:
        return
    owner = database.execute(
        "SELECT user_id, operator_name, machine_id FROM scan_sessions WHERE station_id = ?",
        (station_id,),
    ).fetchone()
    if owner and owner["machine_id"] and owner["user_id"] not in {None, actor_user_id}:
        raise ApiError(f"该扫码工位正由“{owner['operator_name']}”使用", 409)


def session_state(database: sqlite3.Connection, station_id: str) -> dict[str, Any]:
    session = database.execute(
        "SELECT * FROM scan_sessions WHERE station_id = ?", (station_id,)
    ).fetchone()
    machine = None
    items: list[dict[str, Any]] = []
    plan = None
    expected_slots: list[dict[str, Any]] = []
    if session and session["machine_id"]:
        machine_row = database.execute(
            """
            SELECT m.*, 0 AS traced, NULL AS reserved_station,
                   pm.name AS product_model_name,
                   pf.id AS product_family_id, pf.product_code AS product_family_code,
                   pf.name AS product_family_name,
                   tp.version AS trace_plan_version, tp.name AS trace_plan_name
            FROM machines m
            LEFT JOIN product_models pm ON pm.id = m.product_model_id
            LEFT JOIN product_families pf ON pf.id = pm.product_family_id
            LEFT JOIN trace_plans tp ON tp.id = m.trace_plan_id
            WHERE m.id = ?
            """,
            (session["machine_id"],),
        ).fetchone()
        if machine_row:
            machine = machine_dict(machine_row)
        plan_id = session["trace_plan_id"] or (machine_row["trace_plan_id"] if machine_row else None)
        plan = get_plan_summary(database, plan_id)
        expected_slots = plan["slots"] if plan else []
        item_rows = database.execute(
            """
            SELECT ssi.position, pl.*, pt.part_code, pt.name AS part_name,
                   pt.category_code, pt.category_name,
                   s.supplier_code, s.name AS supplier_name,
                   0 AS used, NULL AS reserved_station
            FROM scan_session_items ssi
            JOIN part_labels pl ON pl.id = ssi.part_label_id
            JOIN part_types pt ON pt.id = pl.part_type_id
            JOIN suppliers s ON s.id = pt.supplier_id
            WHERE ssi.station_id = ?
            ORDER BY ssi.position
            """,
            (station_id,),
        ).fetchall()
        items = []
        for row in item_rows:
            item = {"position": row["position"], **part_label_dict(row)}
            if expected_slots and row["position"] <= len(expected_slots):
                item["slot"] = expected_slots[row["position"] - 1]
            items.append(item)

    required_count = len(expected_slots) if expected_slots else DEFAULT_GENERIC_PART_COUNT
    part_count = len(items)
    if not machine:
        next_expected = "machine"
        current_step = 0
    elif part_count < required_count:
        next_expected = "part"
        current_step = 1 + part_count
    else:
        next_expected = "complete"
        current_step = 1 + required_count

    return {
        "stationId": station_id,
        "stationName": session["station_name"] if session else "",
        "operatorName": session["operator_name"] if session else "",
        "requiredPartCount": required_count,
        "workflowMode": "BOM" if plan else "GENERIC",
        "tracePlan": plan,
        "expectedSlots": expected_slots,
        "nextExpectedSlot": expected_slots[part_count] if expected_slots and part_count < len(expected_slots) else None,
        "totalSteps": 1 + required_count,
        "currentStep": current_step,
        "nextExpected": next_expected,
        "machine": machine,
        "parts": items,
        "updatedAt": session["updated_at"] if session else None,
    }


def fetch_records(
    database: sqlite3.Connection,
    search: str = "",
    limit: int = 300,
    *,
    product_model_id: int | None = None,
    completed_by_user_id: int | None = None,
    statuses: list[str] | None = None,
    generation_batch_id: int | None = None,
    completed_date_from: str = "",
    completed_date_to: str = "",
    record_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    parameters: list[Any] = []
    clauses: list[str] = []
    if search:
        wildcard = f"%{search}%"
        clauses.append("""
        (r.trace_no LIKE ? OR m.sn LIKE ? OR m.model LIKE ?
           OR pm.name LIKE ? OR pf.product_code LIKE ? OR pf.name LIKE ?
           OR r.operator_name LIKE ? OR r.station_name LIKE ?
           OR tp.name LIKE ? OR tp.version LIKE ?
           OR EXISTS (
                SELECT 1 FROM trace_record_parts srp
                JOIN part_labels spl ON spl.id = srp.part_label_id
                JOIN part_types spt ON spt.id = spl.part_type_id
                JOIN suppliers ss ON ss.id = spt.supplier_id
                LEFT JOIN part_label_batches spb ON spb.id = spl.label_batch_id
                WHERE srp.trace_record_id = r.id
                  AND (spl.identification_code LIKE ? OR spt.part_code LIKE ?
                       OR spt.name LIKE ? OR ss.supplier_code LIKE ? OR ss.name LIKE ?
                       OR spl.lot_no LIKE ? OR spl.supplier_batch_no LIKE ?
                       OR spl.source_serial_no LIKE ? OR spb.batch_code LIKE ?)
           )
        )""")
        parameters.extend([wildcard] * 19)
    if product_model_id is not None:
        clauses.append("m.product_model_id = ?")
        parameters.append(product_model_id)
    if completed_by_user_id is not None:
        clauses.append("r.completed_by_user_id = ?")
        parameters.append(completed_by_user_id)
    if statuses:
        status_placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"r.status IN ({status_placeholders})")
        parameters.extend(statuses)
    if generation_batch_id is not None:
        clauses.append("pcs.generation_batch_id = ?")
        parameters.append(generation_batch_id)
    if completed_date_from:
        clauses.append("date(r.completed_at) >= date(?)")
        parameters.append(completed_date_from)
    if completed_date_to:
        clauses.append("date(r.completed_at) <= date(?)")
        parameters.append(completed_date_to)
    if record_ids:
        record_placeholders = ",".join("?" for _ in record_ids)
        clauses.append(f"r.id IN ({record_placeholders})")
        parameters.extend(record_ids)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    record_rows = database.execute(
        f"""
        SELECT r.*, m.sn, m.model, m.product_model_id, m.production_date,
               m.identification_code AS machine_code,
               pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name,
               tp.name AS plan_name, tp.version AS plan_version, tp.model_code AS plan_model_code,
               pcb.id AS generation_batch_id, pcb.batch_code AS generation_batch_code,
               pcb.date_code AS generation_batch_date_code,
               pcb.generated_at AS generation_batch_generated_at
        FROM trace_records r
        JOIN machines m ON m.id = r.machine_id
        LEFT JOIN product_models pm ON pm.id = m.product_model_id
        LEFT JOIN product_families pf ON pf.id = pm.product_family_id
        LEFT JOIN trace_plans tp ON tp.id = r.trace_plan_id
        LEFT JOIN product_code_sets pcs ON pcs.machine_id = m.id
        LEFT JOIN product_code_batches pcb ON pcb.id = pcs.generation_batch_id
        {where_clause}
        ORDER BY r.completed_at DESC, r.id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()
    if not record_rows:
        return []

    record_ids = [row["id"] for row in record_rows]
    placeholders = ",".join("?" for _ in record_ids)
    part_rows = database.execute(
        f"""
        SELECT trp.trace_record_id, trp.position, pl.*, pt.part_code,
               pt.name AS part_name, pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at, plb.generated_by AS batch_generated_by,
               1 AS used, NULL AS reserved_station
        FROM trace_record_parts trp
        JOIN part_labels pl ON pl.id = trp.part_label_id
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE trp.trace_record_id IN ({placeholders})
        ORDER BY trp.trace_record_id, trp.position
        """,
        record_ids,
    ).fetchall()
    grouped_parts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in part_rows:
        grouped_parts[row["trace_record_id"]].append(
            {"position": row["position"], **part_label_dict(row)}
        )

    return [
        {
            "id": row["id"],
            "traceNo": row["trace_no"],
            "machine": {
                "id": row["machine_id"],
                "sn": row["sn"],
                "model": row["model"],
                "productModelId": row["product_model_id"],
                "productModelName": row["product_model_name"],
                "productFamilyId": row["product_family_id"],
                "productFamilyCode": row["product_family_code"],
                "productFamilyName": row["product_family_name"],
                "productionDate": row["production_date"],
                "identificationCode": row["machine_code"],
                "qrUrl": f"/api/machines/{row['machine_id']}/qr",
            },
            "parts": grouped_parts[row["id"]],
            "status": row["status"],
            "tracePlan": {
                "id": row["trace_plan_id"],
                "name": row["plan_name"],
                "version": row["plan_version"],
                "modelCode": row["plan_model_code"],
            } if row["trace_plan_id"] else None,
            "generationBatch": {
                "id": row["generation_batch_id"],
                "batchCode": row["generation_batch_code"],
                "dateCode": row["generation_batch_date_code"],
                "generatedAt": row["generation_batch_generated_at"],
            } if row["generation_batch_id"] else None,
            "stationId": row["station_id"],
            "stationName": row["station_name"],
            "operatorName": row["operator_name"],
            "remarks": row["remarks"] if "remarks" in row.keys() else "",
            "statusReason": row["status_reason"] if "status_reason" in row.keys() else "",
            "statusUpdatedAt": row["status_updated_at"] if "status_updated_at" in row.keys() else None,
            "statusUpdatedByUserId": (
                row["status_updated_by_user_id"]
                if "status_updated_by_user_id" in row.keys() else None
            ),
            "completedByUserId": row["completed_by_user_id"],
            "completedAt": row["completed_at"],
        }
        for row in record_rows
    ]


def compute_batch_inventory_requirements(
    database: sqlite3.Connection,
    trace_plan_id: int,
    quantity: int,
) -> list[dict[str, int]]:
    """Compute per-supplier-batch deduction requirements for a production batch.

    Per-unit usage for a supplier batch equals the number of ``trace_plan_slots``
    in the given active trace plan that bind that supplier inventory batch (see
    design "每套用量（Per_Unit_Usage）的落地口径"). The required deduction for a
    batch of ``quantity`` units is ``quantity × per-unit usage``. Only supplier
    batches whose per-unit usage is greater than 0 (i.e. bound by at least one
    slot) contribute a requirement; slots without a bound supplier batch consume
    nothing.

    Returns a list of ``{"supplier_inventory_batch_id", "part_type_id",
    "per_unit_usage", "required"}`` dicts ordered by supplier batch id. The
    ``part_type_id`` reflects the part the slot expects; requirements are
    aggregated per supplier inventory batch (Requirement 4.1).
    """
    slots = database.execute(
        """
        SELECT supplier_inventory_batch_id, part_type_id
        FROM trace_plan_slots
        WHERE trace_plan_id = ? AND supplier_inventory_batch_id IS NOT NULL
        ORDER BY position
        """,
        (trace_plan_id,),
    ).fetchall()

    per_unit_usage: dict[int, int] = defaultdict(int)
    part_type_by_batch: dict[int, int] = {}
    for slot in slots:
        inventory_batch_id = slot["supplier_inventory_batch_id"]
        per_unit_usage[inventory_batch_id] += 1
        part_type_by_batch.setdefault(inventory_batch_id, slot["part_type_id"])

    requirements: list[dict[str, int]] = []
    for inventory_batch_id in sorted(per_unit_usage):
        usage = per_unit_usage[inventory_batch_id]
        if usage <= 0:
            continue
        requirements.append(
            {
                "supplier_inventory_batch_id": inventory_batch_id,
                "part_type_id": part_type_by_batch[inventory_batch_id],
                "per_unit_usage": usage,
                "required": quantity * usage,
            }
        )
    return requirements


def deduct_batch_inventory(
    database: sqlite3.Connection,
    *,
    trace_plan_id: int,
    product_model_id: int,
    quantity: int,
    production_batch_id: int,
    batch_code: str,
    actor_user_id: int | None,
    timestamp: str,
) -> list[dict[str, int]]:
    """Deduct supplier inventory for a production batch within an open transaction.

    This helper MUST be called inside an already-open ``BEGIN IMMEDIATE``
    transaction (opened by the caller, e.g. the production-batch generation
    endpoint) so that concurrent batch generations serialize on the shared
    supplier batch balances and their cumulative deductions never exceed the
    available stock (Requirement 4.5).

    For each supplier inventory batch bound by the active trace plan with a
    per-unit usage greater than 0, the required deduction is ``quantity ×
    per-unit usage`` (Requirement 4.1). A sufficiency check is performed across
    all required parts first; if any part's required deduction exceeds its
    supplier batch's current available balance, an ``ApiError`` naming the short
    part is raised and no balance is modified and no ``ISSUE`` movement is
    written (Requirement 4.2 — the caller's transaction rolls back, leaving no
    partial deduction). Otherwise each batch balance is reduced and exactly one
    ``ISSUE`` row is written to ``supplier_inventory_movements`` carrying the
    ``production_batch_id`` (Requirements 4.1, 4.3).

    Returns the consumption records (``supplier_inventory_batch_id``,
    ``part_type_id``, ``quantity_consumed``) for the caller to persist as
    ``production_batch_supplier_consumption`` rows.
    """
    requirements = compute_batch_inventory_requirements(
        database, trace_plan_id, quantity
    )

    # Sufficiency check first: any shortfall fails the whole operation before
    # touching any balance or writing any movement (Requirement 4.2).
    inventories: dict[int, sqlite3.Row] = {}
    for requirement in requirements:
        inventory_batch_id = requirement["supplier_inventory_batch_id"]
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
        if inventory["quantity_available"] < requirement["required"]:
            raise ApiError(
                f"{inventory['part_name']} / {inventory['batch_no']} 库存不足："
                f"需要 {requirement['required']}，可用 {inventory['quantity_available']}",
                409,
            )
        inventories[inventory_batch_id] = inventory

    # All parts have sufficient stock: deduct balances and write ISSUE movements.
    consumption: list[dict[str, int]] = []
    for requirement in requirements:
        inventory_batch_id = requirement["supplier_inventory_batch_id"]
        required_quantity = requirement["required"]
        inventory = inventories[inventory_batch_id]
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
                balance_after, product_model_id, production_batch_id,
                actor_user_id, reason, occurred_at
            ) VALUES (?, 'ISSUE', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inventory_batch_id, -required_quantity, balance_after,
                product_model_id, production_batch_id, actor_user_id,
                f"生成 {batch_code} 生产批次",
                timestamp,
            ),
        )
        consumption.append(
            {
                "supplier_inventory_batch_id": inventory_batch_id,
                "part_type_id": requirement["part_type_id"],
                "quantity_consumed": required_quantity,
            }
        )
    return consumption


def production_batch_dict(
    database: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Serialize a ``production_batches`` row for API responses.

    Carries the batch code value, product model, prefix, planned quantity,
    generation time and operator (Requirements 1.3, 1.5, 3.1) plus the QR /
    download URLs served by task 6.2. The batch QR encodes ``PTS:B:{batch_code}``
    so a single code value represents the whole batch (Requirements 11.1, 11.2).
    """
    keys = row.keys()
    # Use the product model columns when the caller already joined them (list
    # endpoints), otherwise resolve them with a single lookup. This avoids an
    # N+1 product query when serializing a whole batch list.
    if "model_code" in keys:
        model_code = row["model_code"]
        product_name = row["product_name"] if "product_name" in keys else None
    else:
        product = database.execute(
            "SELECT model_code, name FROM product_models WHERE id = ?",
            (row["product_model_id"],),
        ).fetchone()
        model_code = product["model_code"] if product else None
        product_name = product["name"] if product else None
    batch_code = row["batch_code"]
    return {
        "id": row["id"],
        "batchCode": batch_code,
        "identificationCode": batch_identification_code(batch_code),
        "productModelId": row["product_model_id"],
        "productModelCode": model_code,
        "productName": product_name,
        "tracePlanId": row["trace_plan_id"],
        "prefix": row["prefix"],
        "plannedQuantity": row["planned_quantity"],
        "generatedBy": row["generated_by"] if "generated_by" in keys else "",
        "generatedByUserId": row["generated_by_user_id"],
        "generatedAt": row["generated_at"],
        "qrUrl": f"/api/production-batches/{row['id']}/qr",
        "downloadUrl": f"/api/production-batches/{row['id']}/qr",
    }


def production_batch_reverse_trace(
    database: sqlite3.Connection,
    production_batch_id: int,
) -> list[dict[str, Any]]:
    """Return the batch-level reverse-trace list for a production batch.

    Each entry names the consumed supplier inventory batch, its supplied part
    and the amount consumed (Requirements 5.1, 5.3, 10.2). When no supplier
    batch was consumed the list is empty rather than an error (Requirement 5.6).
    """
    rows = database.execute(
        """
        SELECT
            pbsc.supplier_inventory_batch_id AS supplier_inventory_batch_id,
            pbsc.part_type_id AS part_type_id,
            pbsc.quantity_consumed AS quantity_consumed,
            sib.batch_no AS batch_no,
            pt.part_code AS part_code,
            pt.name AS part_name
        FROM production_batch_supplier_consumption pbsc
        LEFT JOIN supplier_inventory_batches sib
            ON sib.id = pbsc.supplier_inventory_batch_id
        LEFT JOIN part_types pt ON pt.id = pbsc.part_type_id
        WHERE pbsc.production_batch_id = ?
        ORDER BY pbsc.id ASC
        """,
        (production_batch_id,),
    ).fetchall()
    return [
        {
            "supplierInventoryBatchId": row["supplier_inventory_batch_id"],
            "supplierBatchNo": row["batch_no"],
            "partTypeId": row["part_type_id"],
            "partCode": row["part_code"],
            "partName": row["part_name"],
            "quantityConsumed": row["quantity_consumed"],
        }
        for row in rows
    ]


def supplier_inventory_batch_forward_trace(
    database: sqlite3.Connection,
    supplier_inventory_batch_id: int,
) -> list[dict[str, Any]]:
    """Return the forward-trace list for a supplier inventory batch.

    Each entry names a production batch that consumed the supplier batch and
    carries the batch code value, product model, generation time and the amount
    of this supplier batch it consumed (Requirement 5.5). When no production
    batch consumed it the list is empty rather than an error (Requirement 5.6).
    """
    rows = database.execute(
        """
        SELECT
            pb.id AS production_batch_id,
            pb.batch_code AS batch_code,
            pb.product_model_id AS product_model_id,
            pb.generated_at AS generated_at,
            pm.model_code AS product_model_code,
            pm.name AS product_name,
            pbsc.quantity_consumed AS quantity_consumed
        FROM production_batch_supplier_consumption pbsc
        JOIN production_batches pb ON pb.id = pbsc.production_batch_id
        LEFT JOIN product_models pm ON pm.id = pb.product_model_id
        WHERE pbsc.supplier_inventory_batch_id = ?
        ORDER BY pb.generated_at DESC, pb.id DESC
        """,
        (supplier_inventory_batch_id,),
    ).fetchall()
    return [
        {
            "productionBatchId": row["production_batch_id"],
            "batchCode": row["batch_code"],
            "productModelId": row["product_model_id"],
            "productModelCode": row["product_model_code"],
            "productName": row["product_name"],
            "generatedAt": row["generated_at"],
            "quantityConsumed": row["quantity_consumed"],
        }
        for row in rows
    ]


def production_batch_registration(
    database: sqlite3.Connection,
    production_batch_id: int,
) -> dict[str, Any]:
    """Summarize whether a production batch has a batch registration record.

    A batch has at most one ``batch_trace_records`` row (Requirement 2). This
    surfaces the registration status for the detail view (design 6.2).
    """
    row = database.execute(
        """
        SELECT id, registered_quantity, quality_status, status_reason,
               operator_name, completed_by_user_id, registered_at,
               status_updated_at
        FROM batch_trace_records
        WHERE production_batch_id = ?
        """,
        (production_batch_id,),
    ).fetchone()
    if not row:
        return {"registered": False}
    return {
        "registered": True,
        "id": row["id"],
        "registeredQuantity": row["registered_quantity"],
        "qualityStatus": row["quality_status"],
        "statusReason": row["status_reason"],
        "operatorName": row["operator_name"],
        "completedByUserId": row["completed_by_user_id"],
        "registeredAt": row["registered_at"],
        "statusUpdatedAt": row["status_updated_at"],
    }


def batch_trace_record_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize a batch registration record joined to its production batch.

    Carries the batch code value, product model, registered quantity, the
    batch's generation time, prefix and operator (Requirement 3.2). ``row`` is
    expected to expose the ``batch_trace_records`` columns together with the
    joined ``production_batches`` (``batch_code`` / ``prefix`` /
    ``generated_at`` / ``product_model_id``) and ``product_models``
    (``model_code`` / ``name``) fields.
    """
    keys = row.keys()
    batch_code = row["batch_code"]
    return {
        "id": row["id"],
        "productionBatchId": row["production_batch_id"],
        "batchCode": batch_code,
        "identificationCode": batch_identification_code(batch_code),
        "productModelId": row["product_model_id"],
        "productModelCode": row["product_model_code"] if "product_model_code" in keys else None,
        "productName": row["product_name"] if "product_name" in keys else None,
        "registeredQuantity": row["registered_quantity"],
        "qualityStatus": row["quality_status"],
        "prefix": row["prefix"],
        "generatedAt": row["generated_at"],
        "operatorName": row["operator_name"],
        "registeredAt": row["registered_at"],
    }


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        DATABASE=str(DEFAULT_DATABASE),
        JSON_AS_ASCII=False,
        # Re-read the SPA template from disk when it changes so an in-place
        # update (new markup shipped alongside new static JS) is reflected
        # without requiring a full server restart. Otherwise Jinja caches the
        # compiled template for the process lifetime (debug is off in
        # production) and can serve stale markup against fresh JS, which throws
        # "Cannot set properties of null" when the JS looks for new elements.
        TEMPLATES_AUTO_RELOAD=True,
        SECRET_KEY=os.environ.get("PTS_SECRET_KEY", DEFAULT_SECRET_KEY),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PTS_HTTPS_ONLY", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        AUTH_DISABLED=False,
        BOOTSTRAP_ADMIN_USERNAME=os.environ.get("PTS_BOOTSTRAP_ADMIN_USERNAME", "admin"),
        BOOTSTRAP_ADMIN_PASSWORD=os.environ.get("PTS_BOOTSTRAP_ADMIN_PASSWORD", DEFAULT_BOOTSTRAP_PASSWORD),
        BOOTSTRAP_ADMIN_DISPLAY_NAME=os.environ.get("PTS_BOOTSTRAP_ADMIN_DISPLAY_NAME", "系统管理员"),
        NOW_PROVIDER=now_iso,
        LINGXING_HTTP_CLIENT=None,
        LINGXING_SERVICE_FACTORY=None,
        LINGXING_CLOCK=None,
        LINGXING_SLEEP=None,
        LINGXING_API_BASE_URL=os.environ.get(
            "PTS_LINGXING_API_BASE_URL", DEFAULT_API_BASE_URL
        ),
        LINGXING_ENDPOINTS={
            "token": os.environ.get("PTS_LINGXING_TOKEN_URL", DEFAULT_TOKEN_PATH),
            "refresh_token": os.environ.get(
                "PTS_LINGXING_REFRESH_TOKEN_URL", DEFAULT_REFRESH_TOKEN_PATH
            ),
            # 采购单下单 (setOrders), 快捷入库 (fastReceive) and 查询收货单列表
            # (getOrderList) all have documented, stable routes, so they ship as
            # defaults. 库存同步 resolves each order's 收货单 via getOrderList and
            # then receives it with fastReceive. Only 供应收货 stays opt-in.
            "purchase_order": os.environ.get(
                "PTS_LINGXING_PURCHASE_ORDER_URL", DEFAULT_PURCHASE_ORDER_PATH
            ),
            "inbound_receipt": os.environ.get("PTS_LINGXING_INBOUND_URL", ""),
            "receipt_list": os.environ.get(
                "PTS_LINGXING_RECEIPT_LIST_URL", DEFAULT_RECEIPT_LIST_PATH
            ),
            "inventory_sync": os.environ.get(
                "PTS_LINGXING_INVENTORY_URL", DEFAULT_INVENTORY_RECEIVE_PATH
            ),
        },
        LINGXING_MAX_RETRIES=3,
        LINGXING_REQUEST_TIMEOUT=30,
        LINGXING_RETRY_INTERVAL=2,
    )
    if test_config:
        app.config.update(test_config)
        if test_config.get("TESTING") and "AUTH_DISABLED" not in test_config:
            app.config["AUTH_DISABLED"] = True
    deployment_mode = os.environ.get("PTS_ENV", "development").strip().lower()
    if not app.config.get("TESTING") and deployment_mode == "production":
        if (
            app.config["SECRET_KEY"] in {DEFAULT_SECRET_KEY, EXAMPLE_SECRET_KEY}
            or len(app.config["SECRET_KEY"]) < 32
        ):
            raise RuntimeError("生产模式必须设置至少 32 位的 PTS_SECRET_KEY")
        if app.config["BOOTSTRAP_ADMIN_PASSWORD"] in {
            DEFAULT_BOOTSTRAP_PASSWORD,
            EXAMPLE_BOOTSTRAP_PASSWORD,
        }:
            raise RuntimeError("生产模式必须修改 PTS_BOOTSTRAP_ADMIN_PASSWORD")
    initialize_database(app)
    initialize_auth(app)

    @app.after_request
    def secure_response(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'",
        )
        return response

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify({"ok": False, "message": error.message}), error.status

    @app.errorhandler(ValueError)
    def handle_value_error(error: ValueError):
        return jsonify({"ok": False, "message": str(error)}), 400

    @app.errorhandler(BluetoothCollectionError)
    def handle_bluetooth_error(error: BluetoothCollectionError):
        return jsonify({"ok": False, "message": str(error)}), 503

    @app.errorhandler(LingxingError)
    def handle_lingxing_error(error: LingxingError):
        return jsonify({"ok": False, "message": error.message}), error.status

    @app.errorhandler(sqlite3.IntegrityError)
    def handle_integrity_error(error: sqlite3.IntegrityError):
        app.logger.warning("Database constraint rejected a request: %s", error)
        return jsonify({"ok": False, "message": "数据已存在或已被其他工位使用，请刷新后重试"}), 409

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            message = "接口不存在" if error.code == 404 else str(error.description)
            return jsonify({"ok": False, "message": message}), error.code
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        app.logger.exception("Unhandled traceability error", exc_info=error)
        return jsonify({"ok": False, "message": "系统处理失败，请稍后重试"}), 500

    def success(data: Any = None, status: int = 200):
        return jsonify({"ok": True, "data": data}), status

    @app.get("/")
    def index():
        return render_template("index_v2.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/api/health")
    def health():
        return success({"status": "ok", "time": now_iso()})

    @app.get("/api/bluetooth/status")
    def bluetooth_status():
        operator_id = current_operator_id()
        rows = get_db().execute(
            """
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
            ORDER BY pm.model_code
            """,
            (operator_id, operator_id),
        ).fetchall()
        models = [product_model_dict(row) for row in rows]
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

    @app.post("/api/bluetooth/discover")
    def bluetooth_discover():
        operator_id = current_operator_id()
        prefixes = [
            row["bluetooth_name_prefix"]
            for row in get_db().execute(
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
            ).fetchall()
        ]
        if not BLUETOOTH_OPERATION_LOCK.acquire(blocking=False):
            raise ApiError("另一个蓝牙采集操作正在进行，请稍后重试", 409)
        try:
            return success(discover_v2_devices(name_prefixes=prefixes))
        finally:
            BLUETOOTH_OPERATION_LOCK.release()

    @app.post("/api/bluetooth/read-sn")
    def bluetooth_read_sn():
        payload = request.get_json(silent=True) or {}
        database = get_db()
        operator_id = current_operator_id()
        models = database.execute(
            """
            SELECT pm.*, pf.product_code AS product_family_code,
                   pf.name AS product_family_name, 0 AS finished_product_count,
                   0 AS trace_plan_count
            FROM product_models pm JOIN product_families pf ON pf.id = pm.product_family_id
            WHERE pm.active = 1 AND pf.active = 1 AND pm.identity_source = 'BLUETOOTH'
              AND (? IS NULL OR EXISTS(
                    SELECT 1 FROM user_product_model_permissions permission
                    WHERE permission.user_id = ? AND permission.product_model_id = pm.id
              ))
            ORDER BY length(pm.bluetooth_name_prefix) DESC
            """,
            (operator_id, operator_id),
        ).fetchall()
        selected_model = None
        if payload.get("productModelId") not in {None, ""}:
            try:
                requested_model_id = int(payload.get("productModelId"))
            except (TypeError, ValueError):
                raise ApiError("产品型号无效")
            selected_model = next((row for row in models if row["id"] == requested_model_id), None)
        else:
            device_name = str(payload.get("name") or "")
            selected_model = next(
                (
                    row for row in models
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
        reported_model = str(identity.get("model") or "").strip()
        if reported_model and reported_model.upper() != selected_model["model_code"].upper():
            raise ApiError(
                f"设备上报型号 {reported_model}，与所选型号 {selected_model['model_code']} 不一致",
                409,
            )
        existing = database.execute("SELECT id FROM machines WHERE sn = ?", (identity["sn"],)).fetchone()
        return success(
            {
                **identity,
                "model": selected_model["model_code"],
                "productModel": product_model_dict(selected_model),
                "alreadyRegistered": bool(existing),
                "machineId": existing["id"] if existing else None,
            }
        )

    @app.get("/api/dashboard")
    def dashboard():
        require_admin()
        database = get_db()
        # Derive "today" from the same clock the system stamps records with, so the
        # 今日 metrics agree with the stored timestamps instead of the wall clock.
        timestamp = app.config["NOW_PROVIDER"]()
        try:
            today = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            today = datetime.now().astimezone().date().isoformat()
        stock_rows = database.execute(
            """
            SELECT pt.*, s.supplier_code, s.name AS supplier_name,
                   COUNT(DISTINCT CASE WHEN sib.active = 1 THEN sib.id END) AS batch_count,
                   COALESCE(SUM(CASE WHEN sib.active = 1 THEN sib.quantity_available ELSE 0 END), 0)
                       AS quantity_available
            FROM part_types pt
            JOIN suppliers s ON s.id = pt.supplier_id
            LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
            WHERE pt.active = 1 AND s.active = 1
            GROUP BY pt.id
            HAVING quantity_available = 0
                OR (pt.minimum_stock > 0 AND quantity_available <= pt.minimum_stock)
            ORDER BY CASE WHEN quantity_available = 0 THEN 0 ELSE 1 END,
                     quantity_available ASC, pt.updated_at DESC
            """
        ).fetchall()
        quality_queue = fetch_records(
            database, limit=8, statuses=["HOLD", "ASSEMBLED"]
        )
        # The batch model is where current work happens (走步机 moved to batch
        # traceability), so the board also carries the batch registration queue
        # awaiting quality handling. The legacy per-unit queue above is kept for
        # historical data.
        batch_quality_rows = database.execute(
            """
            SELECT r.id AS id, r.production_batch_id AS production_batch_id,
                   r.registered_quantity AS registered_quantity,
                   r.quality_status AS quality_status,
                   r.operator_name AS operator_name,
                   r.registered_at AS registered_at,
                   b.batch_code AS batch_code, b.prefix AS prefix,
                   b.generated_at AS generated_at,
                   b.product_model_id AS product_model_id,
                   pm.model_code AS product_model_code, pm.name AS product_name
            FROM batch_trace_records r
            JOIN production_batches b ON b.id = r.production_batch_id
            LEFT JOIN product_models pm ON pm.id = b.product_model_id
            WHERE r.quality_status IN ('ASSEMBLED', 'HOLD')
            ORDER BY r.registered_at DESC, r.id DESC
            LIMIT 8
            """
        ).fetchall()
        batch_status_counts = {
            row["quality_status"]: row["n"]
            for row in database.execute(
                "SELECT quality_status, COUNT(*) AS n FROM batch_trace_records "
                "GROUP BY quality_status"
            ).fetchall()
        }
        recent_audit_rows = database.execute(
            """
            SELECT ae.*, u.username AS actor_username, u.display_name AS actor_display_name
            FROM audit_events ae
            LEFT JOIN users u ON u.id = ae.actor_user_id
            ORDER BY ae.occurred_at DESC, ae.id DESC
            LIMIT 8
            """
        ).fetchall()
        counts = {
            "machines": database.execute("SELECT COUNT(*) AS n FROM machines").fetchone()["n"],
            "partLabels": database.execute("SELECT COUNT(*) AS n FROM part_labels").fetchone()["n"],
            "traceRecords": database.execute("SELECT COUNT(*) AS n FROM trace_records").fetchone()["n"],
            "todayRecords": database.execute(
                "SELECT COUNT(*) AS n FROM trace_records WHERE substr(completed_at, 1, 10) = ?", (today,)
            ).fetchone()["n"],
            "activeStations": database.execute(
                "SELECT COUNT(*) AS n FROM scan_sessions WHERE machine_id IS NOT NULL"
            ).fetchone()["n"],
            "activeTracePlans": database.execute(
                "SELECT COUNT(*) AS n FROM trace_plans WHERE status = 'ACTIVE'"
            ).fetchone()["n"],
            "assembledRecords": database.execute(
                "SELECT COUNT(*) AS n FROM trace_records WHERE status = 'ASSEMBLED'"
            ).fetchone()["n"],
            "holdRecords": database.execute(
                "SELECT COUNT(*) AS n FROM trace_records WHERE status = 'HOLD'"
            ).fetchone()["n"],
            "passedRecords": database.execute(
                "SELECT COUNT(*) AS n FROM trace_records WHERE status = 'PASSED'"
            ).fetchone()["n"],
            "todayGenerated": database.execute(
                "SELECT COUNT(*) AS n FROM product_code_sets WHERE substr(generated_at, 1, 10) = ?",
                (today,),
            ).fetchone()["n"],
            "lowStockParts": sum(1 for row in stock_rows if row["quantity_available"] > 0),
            "outOfStockParts": sum(1 for row in stock_rows if row["quantity_available"] <= 0),
            # --- Current (batch) model metrics ---
            "productionBatches": database.execute(
                "SELECT COUNT(*) AS n FROM production_batches"
            ).fetchone()["n"],
            "todayBatches": database.execute(
                "SELECT COUNT(*) AS n FROM production_batches "
                "WHERE substr(generated_at, 1, 10) = ?",
                (today,),
            ).fetchone()["n"],
            "registeredBatches": sum(batch_status_counts.values()),
            "batchAssembled": batch_status_counts.get("ASSEMBLED", 0),
            "batchPassed": batch_status_counts.get("PASSED", 0),
            "batchHold": batch_status_counts.get("HOLD", 0),
            "unregisteredBatches": database.execute(
                """
                SELECT COUNT(*) AS n FROM production_batches pb
                WHERE NOT EXISTS (
                    SELECT 1 FROM batch_trace_records r
                    WHERE r.production_batch_id = pb.id
                )
                """
            ).fetchone()["n"],
            "productionOrders": database.execute(
                "SELECT COUNT(*) AS n FROM production_orders"
            ).fetchone()["n"],
            # Purchase orders operations submitted that the warehouse has not
            # turned into a production order yet (流程第 1 步 backlog).
            "pendingProductionOrders": database.execute(
                """
                SELECT COUNT(*) AS n FROM purchase_orders po
                WHERE NOT EXISTS (
                    SELECT 1 FROM production_orders pro
                    WHERE pro.purchase_order_id = po.id
                )
                """
            ).fetchone()["n"],
            # Production orders still short of their planned quantity (待入库).
            "pendingStockIn": database.execute(
                """
                SELECT COUNT(*) AS n FROM production_orders pro
                JOIN production_batches pb ON pb.id = pro.production_batch_id
                WHERE COALESCE((
                    SELECT SUM(isr.quantity) FROM inbound_scan_records isr
                    WHERE isr.production_order_id = pro.id
                ), 0) < COALESCE(pb.planned_quantity, 0)
                """
            ).fetchone()["n"],
            "finishedGoodsOnHand": database.execute(
                "SELECT COALESCE(SUM(on_hand), 0) AS n FROM product_stock"
            ).fetchone()["n"],
            # Stocked products whose Lingxing inventory sync is not PUSHED.
            "inventorySyncPending": database.execute(
                """
                SELECT COUNT(*) AS n FROM product_stock ps
                LEFT JOIN product_stock_sync pss
                    ON pss.product_model_id = ps.product_model_id
                WHERE pss.sync_status IS NULL OR pss.sync_status <> 'PUSHED'
                """
            ).fetchone()["n"],
        }
        return success(
            {
                "counts": counts,
                "qualityQueue": quality_queue,
                "batchQualityQueue": [
                    batch_trace_record_dict(row) for row in batch_quality_rows
                ],
                "stockAlerts": [part_type_dict(row) for row in stock_rows[:8]],
                "recentActivity": [audit_event_dict(row) for row in recent_audit_rows],
                "recentRecords": fetch_records(database, limit=6),
            }
        )

    @app.get("/api/audit-events")
    def list_audit_events():
        require_admin()
        database = get_db()
        search = clean_text(request.args.get("search"), "搜索内容", max_length=120)
        event_type = clean_text(request.args.get("eventType"), "事件类型", max_length=64).upper()
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            raise ApiError("日志条数必须是整数")
        if not 1 <= limit <= 500:
            raise ApiError("日志条数需在 1-500 之间")

        conditions: list[str] = []
        parameters: list[object] = []
        if search:
            pattern = f"%{search}%"
            conditions.append(
                "(" 
                "ae.event_type LIKE ? OR ae.object_code LIKE ? OR ae.related_object_code LIKE ? OR "
                "ae.operator_name LIKE ? OR ae.station_name LIKE ? OR ae.reason LIKE ? OR "
                "COALESCE(u.username, '') LIKE ? OR COALESCE(u.display_name, '') LIKE ?"
                ")"
            )
            parameters.extend([pattern] * 8)
        if event_type:
            conditions.append("ae.event_type = ?")
            parameters.append(event_type)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = database.execute(
            f"""
            SELECT ae.*, u.username AS actor_username, u.display_name AS actor_display_name
            FROM audit_events ae
            LEFT JOIN users u ON u.id = ae.actor_user_id
            {where_clause}
            ORDER BY ae.occurred_at DESC, ae.id DESC
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        filtered_total = database.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM audit_events ae
            LEFT JOIN users u ON u.id = ae.actor_user_id
            {where_clause}
            """,
            parameters,
        ).fetchone()["n"]
        today = datetime.now().astimezone().date().isoformat()
        summary = database.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN substr(occurred_at, 1, 10) = ? THEN 1 ELSE 0 END) AS today,
                   COUNT(DISTINCT NULLIF(operator_name, '')) AS operators
            FROM audit_events
            """,
            (today,),
        ).fetchone()
        event_types = database.execute(
            """
            SELECT event_type, COUNT(*) AS event_count
            FROM audit_events
            GROUP BY event_type
            ORDER BY event_count DESC, event_type
            """
        ).fetchall()
        return success(
            {
                "items": [audit_event_dict(row) for row in rows],
                "filteredTotal": filtered_total,
                "total": summary["total"],
                "today": summary["today"] or 0,
                "operators": summary["operators"],
                "eventTypes": [
                    {"value": row["event_type"], "count": row["event_count"]}
                    for row in event_types
                ],
                "limit": limit,
            }
        )

    def merged_lingxing_endpoints(database: sqlite3.Connection) -> dict[str, str]:
        endpoints = dict(app.config.get("LINGXING_ENDPOINTS") or {})
        endpoints.update(load_lingxing_endpoint_overrides(database))
        return endpoints

    def lingxing_endpoint_readiness(database: sqlite3.Connection) -> dict[str, bool]:
        """Report which write operations have an endpoint configured.

        Gating is per operation: 采购单下单 works as soon as its own route is set,
        without waiting for the inbound / inventory routes to be confirmed.
        """
        endpoints = merged_lingxing_endpoints(database)
        return {
            operation: bool(str(endpoints.get(operation) or "").strip())
            for operation in ("purchase_order", "inbound_receipt", "inventory_sync")
        }

    def lingxing_write_endpoints_configured() -> bool:
        return all(lingxing_endpoint_readiness(get_db()).values())

    def lingxing_settings_block(database: sqlite3.Connection) -> dict[str, Any]:
        credentials = load_credentials(database)
        endpoints = merged_lingxing_endpoints(database)
        readiness = lingxing_endpoint_readiness(database)
        return {
            **credentials.masked(),
            "configured": credentials.configured,
            "writeEndpointsConfigured": all(readiness.values()),
            "endpointsConfigured": {
                "purchaseOrder": readiness["purchase_order"],
                "inboundReceipt": readiness["inbound_receipt"],
                "inventorySync": readiness["inventory_sync"],
            },
            "endpoints": {
                "purchaseOrder": endpoints.get("purchase_order", ""),
                "inboundReceipt": endpoints.get("inbound_receipt", ""),
                "inventorySync": endpoints.get("inventory_sync", ""),
            },
        }

    @app.get("/api/lingxing/status")
    def lingxing_status():
        # Push availability for operations (and admins) without exposing secrets:
        # the UI uses this to enable/disable the 推送领星 controls.
        require_operations()
        database = get_db()
        credentials = load_credentials(database)
        readiness = lingxing_endpoint_readiness(database)
        return success(
            {
                "configured": credentials.configured,
                "writeEndpointsConfigured": all(readiness.values()),
                # Per-operation flags so each push button is gated on its own
                # endpoint instead of all three.
                "endpointsConfigured": {
                    "purchaseOrder": readiness["purchase_order"],
                    "inboundReceipt": readiness["inbound_receipt"],
                    "inventorySync": readiness["inventory_sync"],
                },
            }
        )

    @app.get("/api/settings")
    def get_settings():
        require_admin()
        database = get_db()
        return success(
            {
                "requireQualityRelease": require_quality_release(database),
                "lingxing": lingxing_settings_block(database),
            }
        )

    @app.put("/api/settings")
    def update_settings():
        require_admin()
        payload = request.get_json(silent=True) or {}
        database = get_db()
        credential_fields = ("appId", "appSecret")
        credential_update = any(field in payload for field in credential_fields)
        submitted_credentials: dict[str, str] = {}
        if credential_update:
            for field in credential_fields:
                value = str(payload.get(field) or "").strip()
                if not 1 <= len(value) <= 256:
                    raise ApiError("领星 appId、appSecret 长度均须为 1-256 个字符")
                submitted_credentials[field] = value
        endpoint_update = "lingxingEndpoints" in payload
        submitted_endpoints: dict[str, str] = {}
        if endpoint_update:
            raw_endpoints = payload.get("lingxingEndpoints") or {}
            if not isinstance(raw_endpoints, dict):
                raise ApiError("领星写入接口格式无效")
            submitted_endpoints = {
                "purchase_order": clean_lingxing_endpoint(raw_endpoints.get("purchaseOrder"), "采购订单写入接口"),
                "inbound_receipt": clean_lingxing_endpoint(raw_endpoints.get("inboundReceipt"), "入库写入接口"),
                "inventory_sync": clean_lingxing_endpoint(raw_endpoints.get("inventorySync"), "库存同步接口"),
            }
        quality_release = parse_bool(
            payload.get("requireQualityRelease"),
            require_quality_release(database),
        )
        database.execute("BEGIN IMMEDIATE")
        try:
            if "requireQualityRelease" in payload:
                active = database.execute(
                    "SELECT COUNT(*) AS n FROM scan_sessions WHERE machine_id IS NOT NULL"
                ).fetchone()["n"]
                if active:
                    raise ApiError("仍有工位正在扫码，请先在对应工位清空当前流程", 409)
            database.execute(
                """
                INSERT INTO system_settings(setting_key, setting_value, updated_at)
                VALUES ('require_quality_release', ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                ("1" if quality_release else "0", now_iso()),
            )
            if credential_update:
                timestamp = app.config["NOW_PROVIDER"]()
                setting_values = {
                    "lingxing.app_id": submitted_credentials["appId"],
                    "lingxing.app_secret": submitted_credentials["appSecret"],
                }
                database.executemany(
                    """
                    INSERT INTO app_settings(
                        setting_key, setting_value, is_secret, updated_by,
                        updated_by_user_id, updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value,
                        is_secret = 1,
                        updated_by = excluded.updated_by,
                        updated_by_user_id = excluded.updated_by_user_id,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (
                            key,
                            value,
                            current_actor_name("系统管理员"),
                            current_actor_id(),
                            timestamp,
                        )
                        for key, value in setting_values.items()
                    ],
                )
            if endpoint_update:
                endpoint_timestamp = app.config["NOW_PROVIDER"]()
                database.executemany(
                    """
                    INSERT INTO app_settings(
                        setting_key, setting_value, is_secret, updated_by,
                        updated_by_user_id, updated_at
                    ) VALUES (?, ?, 0, ?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value,
                        is_secret = 0,
                        updated_by = excluded.updated_by,
                        updated_by_user_id = excluded.updated_by_user_id,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (
                            LINGXING_ENDPOINT_SETTING_KEYS[operation],
                            value,
                            current_actor_name("系统管理员"),
                            current_actor_id(),
                            endpoint_timestamp,
                        )
                        for operation, value in submitted_endpoints.items()
                    ],
                )
            record_audit_event(
                database,
                "GENERIC_WORKFLOW_CHANGED",
                "SYSTEM_SETTING",
                "require_quality_release",
                payload={
                    "requireQualityRelease": quality_release,
                    **(
                        {
                            "lingxing": {
                                key: mask_secret(value)
                                for key, value in submitted_credentials.items()
                            }
                        }
                        if credential_update
                        else {}
                    ),
                },
            )
            database.commit()
            if credential_update:
                LINGXING_TOKEN_CACHE.clear()
        except Exception:
            database.rollback()
            raise
        return success(
            {
                "requireQualityRelease": quality_release,
                "lingxing": lingxing_settings_block(database),
            }
        )

    @app.get("/api/product-families")
    def list_product_families():
        operator_id = current_operator_id()
        rows = get_db().execute(
            """
            SELECT pf.*,
                   (SELECT COUNT(*) FROM product_models pm
                    WHERE pm.product_family_id = pf.id) AS model_count
            FROM product_families pf
            WHERE (? IS NULL OR EXISTS(
                SELECT 1
                FROM product_models scoped_model
                JOIN user_product_model_permissions permission
                  ON permission.product_model_id = scoped_model.id
                WHERE permission.user_id = ?
                  AND scoped_model.product_family_id = pf.id
            ))
            ORDER BY pf.active DESC, pf.product_code COLLATE NOCASE
            """,
            (operator_id, operator_id),
        ).fetchall()
        return success([product_family_dict(row) for row in rows])

    @app.post("/api/product-families")
    def create_product_family():
        require_admin()
        payload = request.get_json(silent=True) or {}
        product_code = normalize_entity_code(payload.get("productCode"), "产品分类编码")
        name = clean_text(payload.get("name"), "产品分类名称", required=True, max_length=80)
        description = clean_text(payload.get("description"), "产品分类说明", max_length=300)
        timestamp = now_iso()
        cursor = get_db().execute(
            """
            INSERT INTO product_families(
                product_code, name, description, active, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            (product_code, name, description, timestamp, timestamp),
        )
        row = get_db().execute(
            "SELECT pf.*, 0 AS model_count FROM product_families pf WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return success(product_family_dict(row), 201)

    @app.put("/api/product-families/<int:family_id>")
    def update_product_family(family_id: int):
        require_admin()
        payload = request.get_json(silent=True) or {}
        database = get_db()
        current = database.execute("SELECT * FROM product_families WHERE id = ?", (family_id,)).fetchone()
        if not current:
            raise ApiError("产品分类不存在", 404)
        name = clean_text(payload.get("name", current["name"]), "产品分类名称", required=True, max_length=80)
        description = clean_text(payload.get("description", current["description"]), "产品分类说明", max_length=300)
        active = 1 if parse_bool(payload.get("active"), bool(current["active"])) else 0
        database.execute(
            "UPDATE product_families SET name = ?, description = ?, active = ?, updated_at = ? WHERE id = ?",
            (name, description, active, now_iso(), family_id),
        )
        row = database.execute(
            """
            SELECT pf.*, (SELECT COUNT(*) FROM product_models pm
                          WHERE pm.product_family_id = pf.id) AS model_count
            FROM product_families pf WHERE pf.id = ?
            """,
            (family_id,),
        ).fetchone()
        return success(product_family_dict(row))

    @app.get("/api/product-models")
    def list_product_models():
        family_id = request.args.get("familyId", type=int)
        active_only = parse_bool(request.args.get("activeOnly"), False)
        operator_id = current_operator_id()
        rows = get_db().execute(
            """
            SELECT pm.*, pf.product_code AS product_family_code,
                   pf.name AS product_family_name,
                   (SELECT COUNT(*) FROM machines m
                    WHERE m.product_model_id = pm.id) AS finished_product_count,
                   (SELECT COUNT(*) FROM trace_plans tp
                    WHERE tp.product_model_id = pm.id) AS trace_plan_count
            FROM product_models pm
            JOIN product_families pf ON pf.id = pm.product_family_id
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

    @app.post("/api/product-models")
    def create_product_model():
        require_admin()
        payload = request.get_json(silent=True) or {}
        try:
            family_id = int(payload.get("productFamilyId"))
        except (TypeError, ValueError):
            raise ApiError("请选择产品分类")
        database = get_db()
        family = database.execute(
            "SELECT id FROM product_families WHERE id = ? AND active = 1", (family_id,)
        ).fetchone()
        if not family:
            raise ApiError("产品分类不存在或已停用")
        model_code = normalize_entity_code(payload.get("modelCode"), "产品型号编码")
        name = clean_text(payload.get("name"), "产品型号名称", required=True, max_length=100)
        serial_prefix = clean_text(payload.get("serialPrefix"), "序列号前缀", max_length=40).upper()
        identity_source = str(payload.get("identitySource") or "SCANNER").strip().upper()
        if identity_source not in {"BLUETOOTH", "SCANNER", "MANUAL"}:
            raise ApiError("识别方式只能是蓝牙、扫码枪或手工录入")
        bluetooth_prefix = clean_text(
            payload.get("bluetoothNamePrefix"), "蓝牙名称前缀", max_length=40
        ).upper()
        if identity_source == "BLUETOOTH" and not bluetooth_prefix:
            bluetooth_prefix = serial_prefix or model_code
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

    @app.put("/api/product-models/<int:model_id>")
    def update_product_model(model_id: int):
        require_admin()
        payload = request.get_json(silent=True) or {}
        database = get_db()
        current = database.execute("SELECT * FROM product_models WHERE id = ?", (model_id,)).fetchone()
        if not current:
            raise ApiError("产品型号不存在", 404)
        try:
            family_id = int(payload.get("productFamilyId", current["product_family_id"]))
        except (TypeError, ValueError):
            raise ApiError("请选择产品分类")
        if not database.execute("SELECT id FROM product_families WHERE id = ?", (family_id,)).fetchone():
            raise ApiError("产品分类不存在")
        name = clean_text(payload.get("name", current["name"]), "产品型号名称", required=True, max_length=100)
        serial_prefix = clean_text(
            payload.get("serialPrefix", current["serial_prefix"]), "序列号前缀", max_length=40
        ).upper()
        identity_source = str(payload.get("identitySource", current["identity_source"])).strip().upper()
        if identity_source not in {"BLUETOOTH", "SCANNER", "MANUAL"}:
            raise ApiError("识别方式只能是蓝牙、扫码枪或手工录入")
        bluetooth_prefix = clean_text(
            payload.get("bluetoothNamePrefix", current["bluetooth_name_prefix"]),
            "蓝牙名称前缀",
            max_length=40,
        ).upper()
        if identity_source == "BLUETOOTH" and not bluetooth_prefix:
            bluetooth_prefix = serial_prefix or current["model_code"]
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
            """
            SELECT pm.*, pf.product_code AS product_family_code,
                   pf.name AS product_family_name,
                   (SELECT COUNT(*) FROM machines m
                    WHERE m.product_model_id = pm.id) AS finished_product_count,
                   (SELECT COUNT(*) FROM trace_plans tp
                    WHERE tp.product_model_id = pm.id) AS trace_plan_count
            FROM product_models pm JOIN product_families pf ON pf.id = pm.product_family_id
            WHERE pm.id = ?
            """,
            (model_id,),
        ).fetchone()
        return success(product_model_dict(row))

    @app.delete("/api/product-models/<int:model_id>")
    def delete_product_model(model_id: int):
        require_operations()
        database = get_db()
        product = database.execute(
            "SELECT * FROM product_models WHERE id = ?", (model_id,)
        ).fetchone()
        if not product:
            raise ApiError("产品型号不存在", 404)
        actor = current_user()
        actor_role = actor["role"] if actor else ""
        if actor_role != "ADMIN" and product["created_by_user_id"] != current_actor_id():
            raise ApiError("只能删除自己创建的产品", 403)
        # Only "unused" products may be deleted: no generated QR code sets/batches
        # and no finished-product (machine) or trace records, to preserve
        # traceability of anything already produced.
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

    def product_image_dir() -> Path:
        """Directory holding uploaded product pictures (next to the database)."""
        directory = Path(app.config["DATABASE"]).resolve().parent / "product-images"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @app.post("/api/product-images")
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
        extension, mimetype = detect_product_image_type(data)
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
            },
            201,
        )

    @app.get("/api/product-images/<path:filename>")
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

    @app.get("/api/product-attribute-columns")
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

    @app.get("/api/products")
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

    @app.post("/api/products")
    def create_product():
        # ADMIN keeps the full manufacturing product (with a BOM); OPERATIONS may
        # register their own lightweight products (name + optional SKU, no BOM),
        # which are scoped to their creator.
        require_operations()
        payload = request.get_json(silent=True) or {}
        name = clean_text(payload.get("name"), "产品名称", required=True, max_length=100)
        actor_user_id = current_actor_id()
        actor = current_user()
        actor_role = actor["role"] if actor else ""
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
            timestamp = app.config["NOW_PROVIDER"]()
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
        timestamp = app.config["NOW_PROVIDER"]()
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

    @app.put("/api/products/<int:product_model_id>")
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
        timestamp = app.config["NOW_PROVIDER"]()
        # Extended profile: merge the submitted columns over the stored ones so a
        # partial save never wipes fields the form did not send.
        try:
            stored_attributes = json.loads(product["attributes_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            stored_attributes = {}
        if not isinstance(stored_attributes, dict):
            stored_attributes = {}
        attributes_changed = "attributes" in payload
        merged_attributes = clean_product_attributes(
            {**stored_attributes, **clean_product_attributes(payload.get("attributes"))}
        )
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

    @app.post("/api/products/<int:product_model_id>/code-sets")
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
        timestamp = app.config["NOW_PROVIDER"]()
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
        return success([product_code_set_dict(database, row) for row in rows], 201)

    @app.post("/api/production-batches")
    def create_production_batch():
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

        timestamp = app.config["NOW_PROVIDER"]()
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

    @app.get("/api/production-batches")
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

    @app.get("/api/production-batches/<int:batch_id>")
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

    @app.get("/api/production-batches/<int:batch_id>/qr")
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

    @app.post("/api/batch-entry/scan")
    def batch_entry_scan():
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

        timestamp = app.config["NOW_PROVIDER"]()
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

        timestamp = app.config["NOW_PROVIDER"]()
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

    @app.post("/api/batch-trace-records/<int:record_id>/pass")
    def pass_batch_trace_record(record_id: int):
        # ADMIN releases a batch registration record: ASSEMBLED -> PASSED
        # (Requirement 6.2). A non-ASSEMBLED record is rejected unchanged
        # (Requirement 6.5).
        return success(transition_batch_quality(record_id, "PASSED", ""))

    @app.post("/api/batch-trace-records/<int:record_id>/hold")
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

    @app.get("/api/batch-trace-records")
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
            """,
            parameters,
        ).fetchall()
        return success([batch_trace_record_dict(row) for row in rows])

    @app.post("/api/batch-trace/query")
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

    @app.get("/api/product-code-sets")
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
        return success([product_code_set_dict(database, row) for row in rows])

    @app.get("/api/product-code-batches")
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

    @app.get("/api/product-code-batches/<int:generation_batch_id>")
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
                "sets": [product_code_set_dict(database, row) for row in rows],
                "pagination": {
                    "page": page,
                    "pageSize": page_size,
                    "total": total,
                    "totalPages": max(1, (total + page_size - 1) // page_size),
                },
            }
        )

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
            for row in rows:
                data = product_code_set_dict(database, row)
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
                writer.writerow(
                    [batch_folder, data["setCode"], "产品主码", 0, data["productName"], "", data["machine"]["identificationCode"]]
                )
                for part in data["parts"]:
                    archive.writestr(
                        f"{folder}/{part['position']:02d}-{safe_archive_name(part['partName'], '产品部件')}.svg",
                        make_qr_svg(part["identificationCode"]),
                    )
                    writer.writerow(
                        [batch_folder, data["setCode"], "产品部件", part["position"], part["partName"], part["supplierName"], part["identificationCode"]]
                    )
            archive.writestr(
                f"{root_folder + '/' if root_folder else ''}二维码清单.csv",
                "\ufeff" + manifest.getvalue(),
            )
        output.seek(0)
        return output

    @app.get("/api/product-code-batches/<int:generation_batch_id>/qrcodes.zip")
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

    @app.get("/api/products/<int:product_model_id>/qrcodes.zip")
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

    @app.get("/api/product-code-sets/<int:code_set_id>/qrcodes.zip")
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
            writer.writerow(["产品主码", 0, data["productName"], "", data["machine"]["identificationCode"]])
            for part in data["parts"]:
                filename = f"{part['position']:02d}-产品部件.svg"
                archive.writestr(filename, make_qr_svg(part["identificationCode"]))
                writer.writerow(
                    ["产品部件", part["position"], part["partName"], part["supplierName"], part["identificationCode"]]
                )
            archive.writestr("二维码清单.csv", "\ufeff" + manifest.getvalue())
        output.seek(0)
        return send_file(
            output,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{data['setCode']}-二维码.zip",
        )

    @app.get("/api/suppliers")
    def list_suppliers():
        operator_id = current_operator_id()
        rows = get_db().execute(
            """
            SELECT s.*,
                   COUNT(DISTINCT pt.id) AS part_count,
                   COUNT(DISTINCT sib.id) AS batch_count,
                   COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
            FROM suppliers s
            LEFT JOIN part_types pt ON pt.supplier_id = s.id
            LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
            WHERE (? IS NULL OR EXISTS(
                SELECT 1 FROM user_supplier_permissions permission
                WHERE permission.user_id = ? AND permission.supplier_id = s.id
            ))
            GROUP BY s.id
            ORDER BY s.created_at DESC, s.id DESC
            """,
            (operator_id, operator_id),
        ).fetchall()
        return success([supplier_dict(row) for row in rows])

    @app.post("/api/suppliers")
    def create_supplier():
        require_admin()
        payload = request.get_json(silent=True) or {}
        code = normalize_entity_code(payload.get("supplierCode"), "供应商编码")
        name = clean_text(payload.get("name"), "供应商名称", required=True, max_length=100)
        contact = clean_text(payload.get("contact"), "联系人", max_length=50)
        phone = clean_text(payload.get("phone"), "联系电话", max_length=50)
        database = get_db()
        timestamp = app.config["NOW_PROVIDER"]()
        cursor = database.execute(
            """
            INSERT INTO suppliers(
                supplier_code, name, contact, phone, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (code, name, contact, phone, timestamp, timestamp),
        )
        row = database.execute("SELECT * FROM suppliers WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return success(supplier_dict(row), 201)

    @app.put("/api/suppliers/<int:supplier_id>")
    def update_supplier(supplier_id: int):
        require_admin()
        database = get_db()
        current = database.execute(
            "SELECT * FROM suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()
        if not current:
            raise ApiError("供应商不存在", 404)
        payload = request.get_json(silent=True) or {}
        code = normalize_entity_code(
            payload.get("supplierCode", current["supplier_code"]), "供应商编码"
        )
        name = clean_text(
            payload.get("name", current["name"]), "供应商名称", required=True, max_length=100
        )
        contact = clean_text(payload.get("contact", current["contact"]), "联系人", max_length=50)
        phone = clean_text(payload.get("phone", current["phone"]), "联系电话", max_length=50)
        active = parse_bool(payload.get("active"), bool(current["active"]))
        timestamp = app.config["NOW_PROVIDER"]()
        database.execute(
            """
            UPDATE suppliers
            SET supplier_code = ?, name = ?, contact = ?, phone = ?, active = ?, updated_at = ?
            WHERE id = ?
            """,
            (code, name, contact, phone, int(active), timestamp, supplier_id),
        )
        record_audit_event(
            database,
            "SUPPLIER_UPDATED",
            "SUPPLIER",
            code,
            payload={"name": name, "active": active},
            occurred_at=timestamp,
        )
        row = database.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        return success(supplier_dict(row))

    @app.get("/api/suppliers/<int:supplier_id>")
    def get_supplier_detail(supplier_id: int):
        require_admin()
        database = get_db()
        supplier = database.execute(
            """
            SELECT s.*,
                   COUNT(DISTINCT pt.id) AS part_count,
                   COUNT(DISTINCT sib.id) AS batch_count,
                   COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
            FROM suppliers s
            LEFT JOIN part_types pt ON pt.supplier_id = s.id
            LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
            WHERE s.id = ? GROUP BY s.id
            """,
            (supplier_id,),
        ).fetchone()
        if not supplier:
            raise ApiError("供应商不存在", 404)
        part_rows = database.execute(
            """
            SELECT pt.*, s.supplier_code, s.name AS supplier_name,
                   COUNT(sib.id) AS batch_count,
                   COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
            FROM part_types pt
            JOIN suppliers s ON s.id = pt.supplier_id
            LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
            WHERE pt.supplier_id = ?
            GROUP BY pt.id
            ORDER BY pt.active DESC, pt.created_at DESC, pt.id DESC
            """,
            (supplier_id,),
        ).fetchall()
        batch_rows = database.execute(
            """
            SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
                   s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
            FROM supplier_inventory_batches sib
            JOIN part_types pt ON pt.id = sib.part_type_id
            JOIN suppliers s ON s.id = pt.supplier_id
            WHERE pt.supplier_id = ?
            ORDER BY sib.active DESC, sib.received_date DESC, sib.id DESC
            """,
            (supplier_id,),
        ).fetchall()
        usage_rows = database.execute(
            """
            SELECT pm.id AS product_model_id, pm.model_code AS product_code,
                   pm.name AS product_name, pm.active AS product_active,
                   tp.id AS trace_plan_id, pt.id AS part_type_id,
                   pt.part_code, pt.name AS part_name,
                   COUNT(tps.id) AS required_quantity,
                   (SELECT COUNT(*) FROM product_code_sets pcs
                    WHERE pcs.product_model_id = pm.id) AS generated_count
            FROM trace_plan_slots tps
            JOIN trace_plans tp ON tp.id = tps.trace_plan_id
            JOIN product_models pm ON pm.id = tp.product_model_id
            JOIN part_types pt ON pt.id = tps.part_type_id
            WHERE pt.supplier_id = ? AND tp.status = 'ACTIVE'
            GROUP BY pm.id, tp.id, pt.id
            ORDER BY pm.active DESC, pm.name COLLATE NOCASE, pt.name COLLATE NOCASE
            """,
            (supplier_id,),
        ).fetchall()
        used_in_products: dict[int, dict[str, Any]] = {}
        for row in usage_rows:
            item = used_in_products.setdefault(
                row["product_model_id"],
                {
                    "id": row["product_model_id"],
                    "productCode": row["product_code"],
                    "name": row["product_name"],
                    "active": bool(row["product_active"]),
                    "tracePlanId": row["trace_plan_id"],
                    "generatedCount": row["generated_count"],
                    "requiredQuantity": 0,
                    "parts": [],
                },
            )
            item["requiredQuantity"] += row["required_quantity"]
            item["parts"].append(
                {
                    "id": row["part_type_id"],
                    "partCode": row["part_code"],
                    "name": row["part_name"],
                    "requiredQuantity": row["required_quantity"],
                }
            )
        return success(
            {
                "supplier": supplier_dict(supplier),
                "parts": [part_type_dict(row) for row in part_rows],
                "batches": [supplier_inventory_batch_dict(row) for row in batch_rows],
                "usedInProducts": list(used_in_products.values()),
            }
        )

    @app.get("/api/part-types")
    def list_part_types():
        operator_id = current_operator_id()
        rows = get_db().execute(
            """
            SELECT pt.*, s.supplier_code, s.name AS supplier_name,
                   COUNT(sib.id) AS batch_count,
                   COALESCE(SUM(sib.quantity_available), 0) AS quantity_available
            FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id
            LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
            WHERE (? IS NULL OR EXISTS(
                SELECT 1 FROM user_supplier_permissions permission
                WHERE permission.user_id = ? AND permission.supplier_id = pt.supplier_id
            ))
            GROUP BY pt.id
            ORDER BY pt.created_at DESC, pt.id DESC
            """,
            (operator_id, operator_id),
        ).fetchall()
        return success([part_type_dict(row) for row in rows])

    @app.post("/api/part-types")
    def create_part_type():
        require_admin()
        payload = request.get_json(silent=True) or {}
        part_code = normalize_entity_code(payload.get("partCode"), "部件编码")
        name = clean_text(payload.get("name"), "部件名称", required=True, max_length=100)
        category_code = normalize_entity_code(
            payload.get("categoryCode") or part_code, "部件分类编码"
        )
        category_name = clean_text(
            payload.get("categoryName") or name, "部件分类名称", required=True, max_length=100
        )
        specification = clean_text(payload.get("specification"), "规格型号", max_length=150)
        try:
            minimum_stock = int(payload.get("minimumStock", 0))
        except (TypeError, ValueError) as error:
            raise ApiError("安全库存必须是整数") from error
        if not 0 <= minimum_stock <= 10_000_000:
            raise ApiError("安全库存需在 0-10000000 之间")
        try:
            supplier_id = int(payload.get("supplierId"))
        except (TypeError, ValueError):
            raise ApiError("请选择供应商")
        database = get_db()
        supplier = database.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if not supplier:
            raise ApiError("供应商不存在")
        existing_category = database.execute(
            "SELECT category_name FROM part_types WHERE category_code = ? LIMIT 1",
            (category_code,),
        ).fetchone()
        if existing_category:
            category_name = existing_category["category_name"]
        timestamp = app.config["NOW_PROVIDER"]()
        cursor = database.execute(
            """
            INSERT INTO part_types(
                part_code, name, category_code, category_name,
                specification, minimum_stock, supplier_id, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                part_code, name, category_code, category_name,
                specification, minimum_stock, supplier_id, timestamp, timestamp,
            ),
        )
        row = database.execute(
            """
            SELECT pt.*, s.supplier_code, s.name AS supplier_name
            FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id WHERE pt.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        return success(part_type_dict(row), 201)

    @app.put("/api/part-types/<int:part_type_id>")
    def update_part_type(part_type_id: int):
        require_admin()
        database = get_db()
        current = database.execute(
            "SELECT * FROM part_types WHERE id = ?", (part_type_id,)
        ).fetchone()
        if not current:
            raise ApiError("供应部件不存在", 404)
        payload = request.get_json(silent=True) or {}
        part_code = normalize_entity_code(
            payload.get("partCode", current["part_code"]), "部件编码"
        )
        name = clean_text(
            payload.get("name", current["name"]), "部件名称", required=True, max_length=100
        )
        specification = clean_text(
            payload.get("specification", current["specification"]), "规格型号", max_length=150
        )
        try:
            minimum_stock = int(payload.get("minimumStock", current["minimum_stock"]))
        except (TypeError, ValueError) as error:
            raise ApiError("安全库存必须是整数") from error
        if not 0 <= minimum_stock <= 10_000_000:
            raise ApiError("安全库存需在 0-10000000 之间")
        active = parse_bool(payload.get("active"), bool(current["active"]))
        timestamp = app.config["NOW_PROVIDER"]()
        database.execute(
            """
            UPDATE part_types
            SET part_code = ?, name = ?, category_code = ?, category_name = ?,
                specification = ?, minimum_stock = ?, active = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                part_code, name, part_code, name, specification, minimum_stock,
                int(active), timestamp, part_type_id,
            ),
        )
        record_audit_event(
            database,
            "SUPPLIER_PART_UPDATED",
            "PART_TYPE",
            part_code,
            payload={"name": name, "active": active, "minimumStock": minimum_stock},
            occurred_at=timestamp,
        )
        row = database.execute(
            """
            SELECT pt.*, s.supplier_code, s.name AS supplier_name
            FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id
            WHERE pt.id = ?
            """,
            (part_type_id,),
        ).fetchone()
        return success(part_type_dict(row))

    @app.get("/api/supplier-inventory-batches")
    def list_supplier_inventory_batches():
        require_admin()
        database = get_db()
        clauses: list[str] = []
        parameters: list[Any] = []
        for argument, column, label in (
            (request.args.get("supplierId"), "s.id", "供应商"),
            (request.args.get("partTypeId"), "pt.id", "部件"),
        ):
            if argument in (None, ""):
                continue
            try:
                parameters.append(int(argument))
            except (TypeError, ValueError) as error:
                raise ApiError(f"{label}参数无效") from error
            clauses.append(f"{column} = ?")
        if request.args.get("available") == "1":
            clauses.extend(["sib.active = 1", "sib.quantity_available > 0", "pt.active = 1", "s.active = 1"])
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = database.execute(
            f"""
            SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
                   s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
            FROM supplier_inventory_batches sib
            JOIN part_types pt ON pt.id = sib.part_type_id
            JOIN suppliers s ON s.id = pt.supplier_id
            {where_clause}
            ORDER BY s.name, pt.name, sib.active DESC, sib.received_date DESC, sib.id DESC
            """,
            parameters,
        ).fetchall()
        return success([supplier_inventory_batch_dict(row) for row in rows])

    @app.post("/api/supplier-inventory-batches")
    def create_supplier_inventory_batch():
        require_admin()
        payload = request.get_json(silent=True) or {}
        try:
            part_type_id = int(payload.get("partTypeId"))
            quantity = int(payload.get("quantity"))
        except (TypeError, ValueError) as error:
            raise ApiError("请选择部件并填写到货数量") from error
        if not 1 <= quantity <= 10_000_000:
            raise ApiError("到货数量需为 1-10000000")
        batch_no = normalize_entity_code(payload.get("batchNo"), "供应批次号")
        production_date = clean_text(payload.get("productionDate"), "生产日期", max_length=20)
        received_date = clean_text(
            payload.get("receivedDate"), "到货日期", required=True, max_length=20
        )
        remarks = clean_text(payload.get("remarks"), "备注", max_length=200)
        database = get_db()
        part = database.execute(
            """
            SELECT pt.*, s.active AS supplier_active
            FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id
            WHERE pt.id = ?
            """,
            (part_type_id,),
        ).fetchone()
        if not part:
            raise ApiError("供应部件不存在", 404)
        if not part["active"] or not part["supplier_active"]:
            raise ApiError("供应商或部件已停用", 409)
        timestamp = app.config["NOW_PROVIDER"]()
        actor_user_id = current_actor_id()
        database.execute("BEGIN IMMEDIATE")
        try:
            cursor = database.execute(
                """
                INSERT INTO supplier_inventory_batches(
                    part_type_id, batch_no, quantity_received, quantity_available,
                    production_date, received_date, remarks, active,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    part_type_id, batch_no, quantity, quantity,
                    production_date, received_date, remarks, timestamp, timestamp,
                ),
            )
            database.execute(
                """
                INSERT INTO supplier_inventory_movements(
                    inventory_batch_id, movement_type, quantity_change, balance_after,
                    actor_user_id, reason, occurred_at
                ) VALUES (?, 'RECEIPT', ?, ?, ?, ?, ?)
                """,
                (cursor.lastrowid, quantity, quantity, actor_user_id, "供应批次入库", timestamp),
            )
            record_audit_event(
                database,
                "SUPPLIER_BATCH_RECEIVED",
                "SUPPLIER_INVENTORY_BATCH",
                batch_no,
                related_object_code=part["part_code"],
                payload={"partTypeId": part_type_id, "quantity": quantity},
                occurred_at=timestamp,
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        row = database.execute(
            """
            SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
                   s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
            FROM supplier_inventory_batches sib
            JOIN part_types pt ON pt.id = sib.part_type_id
            JOIN suppliers s ON s.id = pt.supplier_id
            WHERE sib.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        return success(supplier_inventory_batch_dict(row), 201)

    @app.put("/api/supplier-inventory-batches/<int:inventory_batch_id>")
    def update_supplier_inventory_batch(inventory_batch_id: int):
        require_admin()
        database = get_db()
        current = database.execute(
            """
            SELECT sib.*, pt.part_code, pt.name AS part_name
            FROM supplier_inventory_batches sib
            JOIN part_types pt ON pt.id = sib.part_type_id
            WHERE sib.id = ?
            """,
            (inventory_batch_id,),
        ).fetchone()
        if not current:
            raise ApiError("供应批次不存在", 404)
        payload = request.get_json(silent=True) or {}
        batch_no = normalize_entity_code(
            payload.get("batchNo", current["batch_no"]), "供应批次号"
        )
        try:
            quantity_received = int(payload.get("quantity", current["quantity_received"]))
        except (TypeError, ValueError) as error:
            raise ApiError("到货数量无效") from error
        quantity_consumed = current["quantity_received"] - current["quantity_available"]
        if quantity_received < quantity_consumed:
            raise ApiError(f"到货数量不能小于已领用数量 {quantity_consumed}", 409)
        if quantity_received > 10_000_000:
            raise ApiError("到货数量不能超过 10000000")
        production_date = clean_text(
            payload.get("productionDate", current["production_date"]), "生产日期", max_length=20
        )
        received_date = clean_text(
            payload.get("receivedDate", current["received_date"]),
            "到货日期", required=True, max_length=20,
        )
        remarks = clean_text(payload.get("remarks", current["remarks"]), "备注", max_length=200)
        active = parse_bool(payload.get("active"), bool(current["active"]))
        quantity_available = quantity_received - quantity_consumed
        quantity_change = quantity_available - current["quantity_available"]
        timestamp = app.config["NOW_PROVIDER"]()
        actor_user_id = current_actor_id()
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                """
                UPDATE supplier_inventory_batches
                SET batch_no = ?, quantity_received = ?, quantity_available = ?,
                    production_date = ?, received_date = ?, remarks = ?,
                    active = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    batch_no, quantity_received, quantity_available,
                    production_date, received_date, remarks,
                    int(active), timestamp, inventory_batch_id,
                ),
            )
            if quantity_change:
                database.execute(
                    """
                    INSERT INTO supplier_inventory_movements(
                        inventory_batch_id, movement_type, quantity_change,
                        balance_after, actor_user_id, reason, occurred_at
                    ) VALUES (?, 'ADJUSTMENT', ?, ?, ?, ?, ?)
                    """,
                    (
                        inventory_batch_id, quantity_change, quantity_available,
                        actor_user_id, "管理员修改批次数量", timestamp,
                    ),
                )
            record_audit_event(
                database,
                "SUPPLIER_BATCH_UPDATED",
                "SUPPLIER_INVENTORY_BATCH",
                batch_no,
                related_object_code=current["part_code"],
                payload={
                    "quantityReceived": quantity_received,
                    "quantityAvailable": quantity_available,
                    "active": active,
                },
                occurred_at=timestamp,
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        row = database.execute(
            """
            SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
                   s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
            FROM supplier_inventory_batches sib
            JOIN part_types pt ON pt.id = sib.part_type_id
            JOIN suppliers s ON s.id = pt.supplier_id
            WHERE sib.id = ?
            """,
            (inventory_batch_id,),
        ).fetchone()
        return success(supplier_inventory_batch_dict(row))

    @app.get("/api/supplier-inventory-batches/<int:inventory_batch_id>/movements")
    def list_supplier_inventory_movements(inventory_batch_id: int):
        require_admin()
        rows = get_db().execute(
            """
            SELECT sim.*, COALESCE(u.display_name, u.username, '') AS actor_name,
                   pm.name AS product_name, pcb.batch_code AS product_code_batch
            FROM supplier_inventory_movements sim
            LEFT JOIN users u ON u.id = sim.actor_user_id
            LEFT JOIN product_models pm ON pm.id = sim.product_model_id
            LEFT JOIN product_code_batches pcb ON pcb.id = sim.product_code_batch_id
            WHERE sim.inventory_batch_id = ?
            ORDER BY sim.occurred_at DESC, sim.id DESC
            """,
            (inventory_batch_id,),
        ).fetchall()
        return success(
            [
                {
                    "id": row["id"],
                    "movementType": row["movement_type"],
                    "quantityChange": row["quantity_change"],
                    "balanceAfter": row["balance_after"],
                    "productName": row["product_name"],
                    "productCodeBatch": row["product_code_batch"],
                    "actorName": row["actor_name"],
                    "reason": row["reason"],
                    "occurredAt": row["occurred_at"],
                }
                for row in rows
            ]
        )

    @app.get("/api/supplier-inventory-batches/<int:inventory_batch_id>/forward-trace")
    def supplier_inventory_batch_forward_trace_view(inventory_batch_id: int):
        # Admin-only forward trace: list every production batch that consumed
        # this supplier batch, each carrying the batch code value, product
        # model, generation time and consumed quantity (Requirement 5.5). A
        # supplier batch with no consumption returns an empty list rather than
        # an error (Requirement 5.6); a missing supplier batch returns 404.
        require_admin()
        database = get_db()
        batch = database.execute(
            "SELECT id FROM supplier_inventory_batches WHERE id = ?",
            (inventory_batch_id,),
        ).fetchone()
        if not batch:
            raise ApiError("供应批次不存在", 404)
        return success(
            supplier_inventory_batch_forward_trace(database, inventory_batch_id)
        )

    @app.get("/api/trace-plans")
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

    @app.post("/api/trace-plans")
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
                raise ApiError("请选择产品型号")
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
                raise ApiError(f"第 {index} 个槽位的部件类型或数量无效")
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

    @app.get("/api/machines")
    def list_machines():
        search = clean_text(request.args.get("search"), "搜索内容", max_length=100)
        wildcard = f"%{search}%"
        operator_id = current_operator_id()
        rows = get_db().execute(
            """
            SELECT m.*, tp.version AS trace_plan_version, tp.name AS trace_plan_name,
                   pm.name AS product_model_name, pf.id AS product_family_id,
                   pf.product_code AS product_family_code, pf.name AS product_family_name,
                   EXISTS(SELECT 1 FROM trace_records tr WHERE tr.machine_id = m.id) AS traced,
                   (SELECT ss.station_name FROM scan_sessions ss WHERE ss.machine_id = m.id LIMIT 1) AS reserved_station
            FROM machines m
            LEFT JOIN trace_plans tp ON tp.id = m.trace_plan_id
            LEFT JOIN product_models pm ON pm.id = m.product_model_id
            LEFT JOIN product_families pf ON pf.id = pm.product_family_id
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

    @app.post("/api/machines")
    def create_machine():
        require_admin()
        payload = request.get_json(silent=True) or {}
        sn = normalize_sn(payload.get("sn"))
        database = get_db()
        product_model = None
        raw_model_id = payload.get("productModelId")
        if raw_model_id not in {None, ""}:
            try:
                product_model_id = int(raw_model_id)
            except (TypeError, ValueError):
                raise ApiError("请选择产品型号")
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
            """
            SELECT m.*, tp.version AS trace_plan_version, tp.name AS trace_plan_name,
                   pm.name AS product_model_name, pf.id AS product_family_id,
                   pf.product_code AS product_family_code, pf.name AS product_family_name,
                   0 AS traced, NULL AS reserved_station
            FROM machines m
            LEFT JOIN trace_plans tp ON tp.id = m.trace_plan_id
            LEFT JOIN product_models pm ON pm.id = m.product_model_id
            LEFT JOIN product_families pf ON pf.id = pm.product_family_id
            WHERE m.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        return success(machine_dict(row), 201)

    @app.get("/api/part-label-batches")
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

    @app.get("/api/part-label-batches/<int:batch_id>")
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

    @app.get("/api/part-label-batches/<int:batch_id>/qrcodes.zip")
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
                writer.writerow(
                    [
                        index, filename, label["identification_code"],
                        label["source_serial_no"], label["created_at"],
                    ]
                )
            archive.writestr("编码清单.csv", "\ufeff" + manifest.getvalue())
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{batch['batch_code']}_二维码.zip",
            mimetype="application/zip",
        )

    @app.get("/api/part-labels")
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

    @app.post("/api/part-labels")
    def create_part_labels():
        require_admin()
        payload = request.get_json(silent=True) or {}
        try:
            part_type_id = int(payload.get("partTypeId"))
            quantity = int(payload.get("quantity", 1))
        except (TypeError, ValueError):
            raise ApiError("部件类型或生成数量无效")
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

    @app.put("/api/part-labels/<int:label_id>")
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

    @app.get("/api/machines/<int:machine_id>/qr")
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

    @app.get("/api/part-labels/<int:label_id>/qr")
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

    @app.get("/api/scan/session")
    def get_scan_session():
        station_id = normalize_station_id(request.args.get("stationId"))
        database = get_db()
        ensure_station_access(database, station_id)
        return success(session_state(database, station_id))

    @app.post("/api/scan/reset")
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

    @app.post("/api/scan/undo")
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

    @app.post("/api/scan")
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

    @app.get("/api/records")
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
        operator_id = current_operator_id()
        if operator_id is not None and row["completed_by_user_id"] != operator_id:
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
        timestamp = app.config["NOW_PROVIDER"]()
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

    @app.put("/api/records/status/bulk")
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

    @app.put("/api/records/<int:record_id>/status")
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

    @app.put("/api/records/<int:record_id>")
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
            timestamp = app.config["NOW_PROVIDER"]()
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

    @app.delete("/api/records/<int:record_id>")
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

    @app.get("/api/genealogy")
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

    @app.get("/api/records/export.xlsx")
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

    # ------------------------------------------------------------------
    # Lingxing integration, production orders and finished-goods inbound
    # ------------------------------------------------------------------
    def business_id(value: object, label: str) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ApiError(f"请选择{label}")
        if isinstance(value, bool):
            raise ApiError(f"{label}无效")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise ApiError(f"{label}无效") from error
        if result <= 0:
            raise ApiError(f"{label}无效")
        return result

    def business_quantity(value: object, label: str) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ApiError(f"{label}必须是 1-999999 之间的整数")
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            raise ApiError(f"{label}必须是 1-999999 之间的整数")
        try:
            quantity = int(value)
        except (TypeError, ValueError) as error:
            raise ApiError(f"{label}必须是 1-999999 之间的整数") from error
        if not 1 <= quantity <= 999999:
            raise ApiError(f"{label}必须是 1-999999 之间的整数")
        return quantity

    def lingxing_service(database: sqlite3.Connection) -> LingxingIntegrationService:
        factory = app.config.get("LINGXING_SERVICE_FACTORY")
        if factory:
            return factory(database)
        return LingxingIntegrationService(
            database=database,
            http_client=app.config.get("LINGXING_HTTP_CLIENT"),
            clock=app.config.get("LINGXING_CLOCK"),
            sleeper=app.config.get("LINGXING_SLEEP"),
            retry_config=RetryConfig(
                max_retries=int(app.config["LINGXING_MAX_RETRIES"]),
                request_timeout=int(app.config["LINGXING_REQUEST_TIMEOUT"]),
                retry_interval=int(app.config["LINGXING_RETRY_INTERVAL"]),
            ),
            endpoints=merged_lingxing_endpoints(database),
            credential_config=app.config.get("LINGXING_CREDENTIALS"),
            api_base_url=app.config.get("LINGXING_API_BASE_URL", DEFAULT_API_BASE_URL),
            token_cache=LINGXING_TOKEN_CACHE,
        )

    def ensure_lingxing_operation_ready(service: Any, operation: str) -> None:
        validator = getattr(service, "ensure_operation_ready", None)
        if callable(validator):
            validator(operation)
        else:
            service.credentials()

    def purchase_order_row(database: sqlite3.Connection, purchase_order_id: int):
        # LEFT JOINs so free-form orders (no supplier / part link) are still
        # returned; the product link is included for the "和产品挂钩" requirement.
        return database.execute(
            """
            SELECT po.*, s.supplier_code, s.name AS supplier_name,
                   s.contact AS supplier_contact, s.phone AS supplier_phone,
                   pt.part_code, pt.name AS part_name, pt.specification,
                   pm.model_code AS product_model_code, pm.name AS product_model_name,
                   (SELECT COALESCE(SUM(ir.quantity), 0) FROM inbound_receipts ir
                     WHERE ir.purchase_order_id = po.id) AS received_quantity,
                   (SELECT MAX(ir.received_at) FROM inbound_receipts ir
                     WHERE ir.purchase_order_id = po.id) AS last_received_at
            FROM purchase_orders po
            LEFT JOIN suppliers s ON s.id = po.supplier_id
            LEFT JOIN part_types pt ON pt.id = po.part_type_id
            LEFT JOIN product_models pm ON pm.id = po.product_model_id
            WHERE po.id = ?
            """,
            (purchase_order_id,),
        ).fetchone()

    def purchase_order_summary(row: sqlite3.Row, fields: dict[str, Any]) -> dict[str, Any]:
        """Flatten the fields the warehouse needs when receiving / producing.

        Everything is derived from what operations entered plus the recorded
        inbound receipts, so the production-order and supplier-receiving pages
        can show one consistent block: identity (SKU / 店铺 / FNSKU / 负责人),
        the dates, and the ordered vs actually-received quantity and amount.
        """
        keys = row.keys()

        def text(*candidates: Any) -> str:
            for candidate in candidates:
                if candidate not in (None, ""):
                    return str(candidate).strip()
            return ""

        ordered_quantity = coerce_export_number(fields.get("实际采购量"))
        if ordered_quantity is None and row["quantity"] not in (None, ""):
            ordered_quantity = coerce_export_number(row["quantity"])
        unit_price = coerce_export_number(fields.get("含税单价"))
        if unit_price is None:
            unit_price = coerce_export_number(fields.get("单价"))
        received_quantity = (
            coerce_export_number(row["received_quantity"])
            if "received_quantity" in keys else None
        ) or 0

        def amount(quantity: Any) -> float | None:
            if unit_price is None or quantity in (None, ""):
                return None
            return round(float(unit_price) * float(quantity), 2)

        return {
            "sku": text(
                fields.get("SKU"),
                row["product_model_code"] if "product_model_code" in keys else None,
                row["part_code"] if "part_code" in keys else None,
            ),
            "productName": text(
                row["product_model_name"] if "product_model_name" in keys else None,
                fields.get("品名"),
                row["part_name"] if "part_name" in keys else None,
            ),
            "shop": text(fields.get("店铺")),
            "fnsku": text(fields.get("FNSKU")),
            # 负责人: the account that placed the order, falling back to the
            # 采购员 typed on the template.
            "owner": text(row["created_by"], fields.get("采购员")),
            "supplierName": text(
                fields.get("供应商"),
                row["supplier_name"] if "supplier_name" in keys else None,
            ),
            "orderedAt": row["created_at"],
            "plannedArrivalAt": text(fields.get("预计到货时间")),
            "actualArrivalAt": (
                row["last_received_at"] if "last_received_at" in keys else None
            ),
            "unitPrice": float(unit_price) if unit_price is not None else None,
            "orderedQuantity": (
                float(ordered_quantity) if ordered_quantity is not None else None
            ),
            "receivedQuantity": float(received_quantity),
            "orderAmount": amount(ordered_quantity),
            "receivedAmount": amount(received_quantity),
        }

    def purchase_order_data(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        try:
            fields = json.loads(row["fields_json"] or "{}") if "fields_json" in keys else {}
        except (json.JSONDecodeError, TypeError):
            fields = {}
        if not isinstance(fields, dict):
            fields = {}
        return {
            "summary": purchase_order_summary(row, fields),
            "id": row["id"],
            "poNo": row["po_no"],
            "supplierId": row["supplier_id"],
            "supplierCode": row["supplier_code"] if "supplier_code" in keys else None,
            "supplierName": row["supplier_name"] if "supplier_name" in keys else None,
            "supplierContact": row["supplier_contact"] if "supplier_contact" in keys else None,
            "supplierPhone": row["supplier_phone"] if "supplier_phone" in keys else None,
            "partTypeId": row["part_type_id"],
            "partCode": row["part_code"] if "part_code" in keys else None,
            "partName": row["part_name"] if "part_name" in keys else None,
            "specification": row["specification"] if "specification" in keys else None,
            "productModelId": row["product_model_id"] if "product_model_id" in keys else None,
            "productModelCode": row["product_model_code"] if "product_model_code" in keys else None,
            "productModelName": row["product_model_name"] if "product_model_name" in keys else None,
            "quantity": row["quantity"],
            "fields": fields,
            "syncStatus": row["sync_status"],
            "pushInProgress": bool(row["push_in_progress"]),
            "lingxingPoId": row["lingxing_po_id"] or None,
            "pushError": row["push_error"],
            "createdBy": row["created_by"],
            "createdByUserId": row["created_by_user_id"],
            "createdAt": row["created_at"],
            "pushedAt": row["pushed_at"],
        }

    def inbound_receipt_row(database: sqlite3.Connection, receipt_id: int):
        return database.execute(
            """
            SELECT ir.*, po.po_no, po.sync_status AS purchase_order_sync_status,
                   po.lingxing_po_id, s.name AS supplier_name,
                   pt.part_code, pt.name AS part_name,
                   pm.name AS product_model_name
            FROM inbound_receipts ir
            JOIN purchase_orders po ON po.id = ir.purchase_order_id
            LEFT JOIN suppliers s ON s.id = po.supplier_id
            LEFT JOIN part_types pt ON pt.id = po.part_type_id
            LEFT JOIN product_models pm ON pm.id = po.product_model_id
            WHERE ir.id = ?
            """,
            (receipt_id,),
        ).fetchone()

    def inbound_receipt_data(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        part_name = row["part_name"]
        if not part_name and "product_model_name" in keys:
            part_name = row["product_model_name"]
        return {
            "id": row["id"],
            "purchaseOrderId": row["purchase_order_id"],
            "poNo": row["po_no"],
            "supplierName": row["supplier_name"],
            "partCode": row["part_code"],
            "partName": part_name,
            "quantity": row["quantity"],
            "receiver": row["receiver"],
            "receiverUserId": row["receiver_user_id"],
            "receivedAt": row["received_at"],
            "syncStatus": row["sync_status"],
            "pushInProgress": bool(row["push_in_progress"]),
            "lingxingInboundId": row["lingxing_inbound_id"] or None,
            "pushError": row["push_error"],
            "createdAt": row["created_at"],
            "pushedAt": row["pushed_at"],
        }

    def external_identifier(response: Any, *keys: str) -> str:
        if not isinstance(response, dict):
            return ""
        containers = [response]
        if isinstance(response.get("data"), dict):
            containers.insert(0, response["data"])
        for container in containers:
            for key in keys:
                value = container.get(key)
                if value not in {None, ""}:
                    return str(value)
        return ""

    def clean_purchase_order_fields(raw_fields: object) -> dict[str, Any]:
        """Normalize the free-form template fields entered by operations.

        Values are keyed by the export column name; unknown columns are dropped
        so the stored payload always maps onto the export template. Numbers and
        booleans are kept as-is; everything else is trimmed text capped at 500
        characters.
        """
        fields: dict[str, Any] = {}
        if raw_fields is None:
            return fields
        if not isinstance(raw_fields, dict):
            raise ApiError("采购单字段格式无效")
        for key, value in raw_fields.items():
            column = str(key).strip()
            if not column or column not in PURCHASE_ORDER_EXPORT_COLUMN_SET:
                continue
            if value is None:
                fields[column] = ""
            elif isinstance(value, bool) or isinstance(value, (int, float)):
                fields[column] = value
            else:
                text = str(value).strip()
                if len(text) > 500:
                    raise ApiError(f"字段“{column}”内容不能超过 500 个字符")
                fields[column] = text
        return fields

    def parse_purchase_order_input(
        database: sqlite3.Connection, payload: dict[str, Any], actor_role: str
    ) -> tuple[int | None, int | None, int | None, int | None, dict[str, Any]]:
        """Validate and normalize purchase-order input shared by create/update.

        Returns ``(product_model_id, supplier_id, part_type_id, quantity,
        fields)``. Supplier / 商品 links are optional; operations may only link
        products they created.
        """
        fields = clean_purchase_order_fields(payload.get("fields"))

        product_model_id: int | None = None
        raw_product = payload.get("productModelId")
        if raw_product not in (None, ""):
            product_model_id = business_id(raw_product, "产品")
            product = database.execute(
                "SELECT id, created_by_user_id FROM product_models WHERE id = ?",
                (product_model_id,),
            ).fetchone()
            if not product:
                raise ApiError("产品不存在", 404)
            if actor_role == "OPERATIONS" and product["created_by_user_id"] != current_actor_id():
                raise ApiError("只能选择自己创建的产品", 403)

        supplier_id: int | None = None
        raw_supplier = payload.get("supplierId")
        if raw_supplier not in (None, ""):
            supplier_id = business_id(raw_supplier, "供应商")
            if not database.execute(
                "SELECT id FROM suppliers WHERE id = ? AND active = 1", (supplier_id,)
            ).fetchone():
                raise ApiError("供应商不存在或已停用", 404)
        part_type_id: int | None = None
        raw_part = payload.get("partTypeId")
        if raw_part not in (None, ""):
            part_type_id = business_id(raw_part, "商品")
            part = database.execute(
                "SELECT id, supplier_id FROM part_types WHERE id = ? AND active = 1",
                (part_type_id,),
            ).fetchone()
            if not part:
                raise ApiError("商品不存在或已停用", 404)
            if supplier_id is not None and int(part["supplier_id"]) != supplier_id:
                raise ApiError("所选商品不属于该供应商", 409)

        quantity: int | None = None
        raw_quantity = payload.get("quantity")
        if raw_quantity not in (None, ""):
            quantity = business_quantity(raw_quantity, "采购数量")
        if quantity is not None and "实际采购量" not in fields:
            fields["实际采购量"] = quantity

        return product_model_id, supplier_id, part_type_id, quantity, fields

    @app.post("/api/purchase-orders")
    def create_purchase_order():
        # Operations fill the purchase-order template directly. Supplier / 商品
        # links are optional; the product link is kept ("和产品挂钩") and every
        # order records its creator (采购人).
        require_operations()
        payload = request.get_json(silent=True) or {}
        database = get_db()
        actor = current_user()
        actor_role = actor["role"] if actor else ""

        product_model_id, supplier_id, part_type_id, quantity, fields = (
            parse_purchase_order_input(database, payload, actor_role)
        )

        if product_model_id is None and part_type_id is None and not fields:
            raise ApiError("请选择产品并填写采购单信息")

        timestamp = app.config["NOW_PROVIDER"]()
        po_no = str(fields.get("采购单号") or "").strip() or (
            f"PO-{re.sub(r'[^0-9]', '', timestamp)[:14]}-{new_event_id()[-8:]}"
        )
        fields["采购单号"] = po_no
        database.execute("BEGIN IMMEDIATE")
        try:
            cursor = database.execute(
                """
                INSERT INTO purchase_orders(
                    po_no, supplier_id, part_type_id, product_model_id, quantity,
                    fields_json, sync_status, created_by, created_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    po_no,
                    supplier_id,
                    part_type_id,
                    product_model_id,
                    quantity,
                    json.dumps(fields, ensure_ascii=False),
                    current_actor_name("系统管理员"),
                    current_actor_id(),
                    timestamp,
                ),
            )
            record_audit_event(
                database,
                "PO_CREATED",
                "PURCHASE_ORDER",
                po_no,
                payload={
                    "productModelId": product_model_id,
                    "supplierId": supplier_id,
                    "partTypeId": part_type_id,
                    "quantity": quantity,
                    "syncStatus": "PENDING",
                },
                occurred_at=timestamp,
            )
            database.commit()
        except sqlite3.IntegrityError as error:
            database.rollback()
            if "po_no" in str(error).lower():
                raise ApiError("采购单号重复，请重试", 409) from error
            raise
        except Exception:
            database.rollback()
            raise
        return success(purchase_order_data(purchase_order_row(database, cursor.lastrowid)), 201)

    def query_purchase_orders() -> list[sqlite3.Row]:
        # Shared list/export query. Read access is shared with warehouse so it
        # can select a source order; operations only ever see their own orders.
        clauses: list[str] = []
        parameters: list[Any] = []
        status = clean_text(request.args.get("syncStatus"), "同步状态", max_length=16).upper()
        if status:
            if status not in {"PENDING", "PUSHED", "FAILED"}:
                raise ApiError("同步状态无效")
            clauses.append("po.sync_status = ?")
            parameters.append(status)
        supplier_value = request.args.get("supplierId")
        if supplier_value not in {None, ""}:
            clauses.append("po.supplier_id = ?")
            parameters.append(business_id(supplier_value, "供应商"))
        range_from = clean_text(request.args.get("from"), "起始时间", max_length=64)
        range_to = clean_text(request.args.get("to"), "结束时间", max_length=64)
        if range_from and range_to and range_from > range_to:
            raise ApiError("起始时间不能晚于结束时间")
        if range_from:
            clauses.append("po.created_at >= ?")
            parameters.append(range_from)
        if range_to:
            clauses.append("po.created_at <= ?")
            parameters.append(range_to)
        actor = current_user()
        if actor and actor["role"] == "OPERATIONS":
            clauses.append("po.created_by_user_id = ?")
            parameters.append(current_actor_id())
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return get_db().execute(
            f"""
            SELECT po.*, s.supplier_code, s.name AS supplier_name,
                   s.contact AS supplier_contact, s.phone AS supplier_phone,
                   pt.part_code, pt.name AS part_name, pt.specification,
                   pm.model_code AS product_model_code, pm.name AS product_model_name,
                   (SELECT COALESCE(SUM(ir.quantity), 0) FROM inbound_receipts ir
                     WHERE ir.purchase_order_id = po.id) AS received_quantity,
                   (SELECT MAX(ir.received_at) FROM inbound_receipts ir
                     WHERE ir.purchase_order_id = po.id) AS last_received_at
            FROM purchase_orders po
            LEFT JOIN suppliers s ON s.id = po.supplier_id
            LEFT JOIN part_types pt ON pt.id = po.part_type_id
            LEFT JOIN product_models pm ON pm.id = po.product_model_id
            {where_clause}
            ORDER BY po.created_at DESC, po.id DESC
            """,
            parameters,
        ).fetchall()

    @app.get("/api/purchase-orders")
    def list_purchase_orders():
        return success([purchase_order_data(row) for row in query_purchase_orders()])

    @app.get("/api/purchase-orders/<int:purchase_order_id>")
    def get_purchase_order(purchase_order_id: int):
        row = purchase_order_row(get_db(), purchase_order_id)
        if not row:
            raise ApiError("采购订单不存在", 404)
        return success(purchase_order_data(row))

    @app.put("/api/purchase-orders/<int:purchase_order_id>")
    def update_purchase_order(purchase_order_id: int):
        # Operations may revise their own orders; admins may revise any. A
        # PUSHED order is locked so the local record stays consistent with what
        # was sent to Lingxing.
        require_operations()
        payload = request.get_json(silent=True) or {}
        database = get_db()
        actor = current_user()
        actor_role = actor["role"] if actor else ""
        existing = purchase_order_row(database, purchase_order_id)
        if not existing:
            raise ApiError("采购订单不存在", 404)
        if actor_role == "OPERATIONS" and existing["created_by_user_id"] != current_actor_id():
            raise ApiError("只能修改自己创建的采购订单", 403)
        if existing["sync_status"] == "PUSHED":
            raise ApiError("已推送的采购订单不可修改", 409)

        product_model_id, supplier_id, part_type_id, quantity, fields = (
            parse_purchase_order_input(database, payload, actor_role)
        )
        if product_model_id is None and part_type_id is None and not fields:
            raise ApiError("请选择产品并填写采购单信息")
        # The document number is immutable; keep the original po_no.
        fields["采购单号"] = existing["po_no"]
        # A revised order that previously failed to push returns to PENDING so it
        # can be retried cleanly.
        next_status = "PENDING" if existing["sync_status"] == "FAILED" else existing["sync_status"]
        timestamp = app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute(
                """
                UPDATE purchase_orders
                SET supplier_id = ?, part_type_id = ?, product_model_id = ?,
                    quantity = ?, fields_json = ?, sync_status = ?,
                    push_error = CASE WHEN ? = 'PENDING' THEN '' ELSE push_error END
                WHERE id = ?
                """,
                (
                    supplier_id,
                    part_type_id,
                    product_model_id,
                    quantity,
                    json.dumps(fields, ensure_ascii=False),
                    next_status,
                    next_status,
                    purchase_order_id,
                ),
            )
            record_audit_event(
                database,
                "PO_UPDATED",
                "PURCHASE_ORDER",
                existing["po_no"],
                payload={
                    "productModelId": product_model_id,
                    "supplierId": supplier_id,
                    "partTypeId": part_type_id,
                    "quantity": quantity,
                },
                occurred_at=timestamp,
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        return success(purchase_order_data(purchase_order_row(database, purchase_order_id)))

    @app.delete("/api/purchase-orders/<int:purchase_order_id>")
    def delete_purchase_order(purchase_order_id: int):
        require_operations()
        database = get_db()
        actor = current_user()
        actor_role = actor["role"] if actor else ""
        existing = purchase_order_row(database, purchase_order_id)
        if not existing:
            raise ApiError("采购订单不存在", 404)
        if actor_role == "OPERATIONS" and existing["created_by_user_id"] != current_actor_id():
            raise ApiError("只能删除自己创建的采购订单", 403)
        referenced = database.execute(
            """
            SELECT (SELECT COUNT(*) FROM inbound_receipts WHERE purchase_order_id = ?)
                 + (SELECT COUNT(*) FROM production_orders WHERE purchase_order_id = ?) AS n
            """,
            (purchase_order_id, purchase_order_id),
        ).fetchone()["n"]
        if referenced:
            raise ApiError("该采购订单已被收货或生产订单引用，无法删除", 409)
        database.execute("BEGIN IMMEDIATE")
        try:
            record_audit_event(
                database,
                "PO_DELETED",
                "PURCHASE_ORDER",
                existing["po_no"],
                payload={"syncStatus": existing["sync_status"]},
            )
            database.execute(
                "DELETE FROM purchase_orders WHERE id = ?", (purchase_order_id,)
            )
            database.commit()
        except sqlite3.IntegrityError as error:
            database.rollback()
            raise ApiError("该采购订单已被其他数据引用，无法删除", 409) from error
        except Exception:
            database.rollback()
            raise
        return success({"id": purchase_order_id, "poNo": existing["po_no"]})

    def purchase_order_lingxing_order_sn(
        row: sqlite3.Row,
        fields: dict[str, Any],
        requested: object = None,
    ) -> str:
        """Resolve the Lingxing 采购单号 (``order_sn``) this local order maps to.

        采购单下单 acts on an order that already exists in Lingxing, so the push
        needs that order's number rather than any local id. Callers may pass it
        explicitly; otherwise a previously recorded number is reused, then the
        采购单号 typed on the template, and finally our own document number
        (which is what the export writes into Lingxing).
        """
        for candidate in (
            requested,
            row["lingxing_po_id"] if "lingxing_po_id" in row.keys() else "",
            fields.get("采购单号"),
            row["po_no"],
        ):
            text = str(candidate or "").strip()
            if not text:
                continue
            if len(text) > 64:
                raise ApiError("领星采购单号不能超过 64 个字符")
            return text
        raise ApiError("缺少领星采购单号，无法执行采购单下单")

    def push_purchase_order_record(
        purchase_order_id: int,
        requested_order_sn: object = None,
    ) -> dict[str, Any]:
        require_operations()
        database = get_db()
        row = purchase_order_row(database, purchase_order_id)
        if not row:
            raise ApiError("采购订单不存在", 404)
        if row["sync_status"] == "PUSHED":
            return {**purchase_order_data(row), "message": "该采购订单已推送"}
        service = lingxing_service(database)
        ensure_lingxing_operation_ready(service, "purchase_order")
        try:
            po_fields = json.loads(row["fields_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            po_fields = {}
        if not isinstance(po_fields, dict):
            po_fields = {}
        # Resolved before the in-progress guard so a missing/invalid order_sn
        # never leaves the record flagged as pushing.
        order_sn = purchase_order_lingxing_order_sn(row, po_fields, requested_order_sn)
        database.execute("BEGIN IMMEDIATE")
        try:
            current = database.execute(
                "SELECT sync_status, push_in_progress, push_started_at "
                "FROM purchase_orders WHERE id = ?",
                (purchase_order_id,),
            ).fetchone()
            if current["sync_status"] == "PUSHED":
                database.rollback()
                return {**purchase_order_data(purchase_order_row(database, purchase_order_id)), "message": "该采购订单已推送"}
            guard_started_at = app.config["NOW_PROVIDER"]()
            if current["push_in_progress"] and not guard_is_stale(
                current["push_started_at"], guard_started_at, PUSH_GUARD_TIMEOUT
            ):
                raise ApiError("采购订单推送正在进行中", 409)
            # Stamping the start lets a later attempt recover an abandoned guard.
            database.execute(
                "UPDATE purchase_orders SET push_in_progress = 1, push_started_at = ? "
                "WHERE id = ?",
                (guard_started_at, purchase_order_id),
            )
            database.commit()
        except Exception:
            database.rollback()
            raise

        try:
            # 采购单下单 (``setOrders``) moves an existing Lingxing purchase order
            # from 待下单 to 待到货. It is keyed by the Lingxing 采购单号 and answers
            # with an empty ``data`` list, so no object id comes back and the
            # order_sn we sent is what we persist.
            response = service.push("purchase_order", {"order_sn": [order_sn]})
            lingxing_id = (
                external_identifier(response, "poId", "purchaseOrderId", "order_sn", "id")
                or order_sn
            )
            timestamp = app.config["NOW_PROVIDER"]()
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE purchase_orders
                SET sync_status = 'PUSHED', push_in_progress = 0,
                    lingxing_po_id = ?, lingxing_raw_response = ?,
                    push_error = '', pushed_at = ?
                WHERE id = ?
                """,
                (lingxing_id, json.dumps(response, ensure_ascii=False), timestamp, purchase_order_id),
            )
            record_audit_event(
                database,
                "PO_PUSHED",
                "PURCHASE_ORDER",
                row["po_no"],
                payload={
                    "result": "PUSHED",
                    "lingxingPoId": lingxing_id,
                    "orderSn": order_sn,
                },
                occurred_at=timestamp,
            )
            database.commit()
        except Exception as error:
            database.rollback()
            message = error.message if isinstance(error, LingxingError) else str(error)
            timestamp = app.config["NOW_PROVIDER"]()
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE purchase_orders
                SET sync_status = 'FAILED', push_in_progress = 0, push_error = ?
                WHERE id = ?
                """,
                (message, purchase_order_id),
            )
            record_audit_event(
                database,
                "PO_PUSH_FAILED",
                "PURCHASE_ORDER",
                row["po_no"],
                reason=message,
                payload={"result": "FAILED", "error": message},
                occurred_at=timestamp,
            )
            database.commit()
            if isinstance(error, (ApiError, LingxingError)):
                raise
            raise LingxingError(f"领星采购订单推送失败：{message}") from error
        return {**purchase_order_data(purchase_order_row(database, purchase_order_id)), "message": "采购订单已推送"}

    @app.post("/api/purchase-orders/<int:purchase_order_id>/push")
    def push_purchase_order(purchase_order_id: int):
        # ``orderSn`` lets operations name the Lingxing 采购单号 explicitly when it
        # differs from our document number; omitted, it is resolved from the
        # order itself.
        payload = request.get_json(silent=True) or {}
        return success(
            push_purchase_order_record(purchase_order_id, payload.get("orderSn"))
        )

    @app.get("/api/purchase-orders/<int:purchase_order_id>/sync-status")
    def purchase_order_sync_status(purchase_order_id: int):
        require_operations()
        row = purchase_order_row(get_db(), purchase_order_id)
        if not row:
            raise ApiError("采购订单不存在", 404)
        return success(
            {
                "id": row["id"],
                "syncStatus": row["sync_status"],
                "lingxingId": row["lingxing_po_id"] or None,
                "pushedAt": row["pushed_at"],
                "pushError": row["push_error"],
            }
        )

    @app.post("/api/inbound-receipts")
    def create_inbound_receipt():
        require_warehouse()
        payload = request.get_json(silent=True) or {}
        purchase_order_id = business_id(payload.get("purchaseOrderId"), "采购订单")
        quantity = business_quantity(payload.get("quantity"), "入库数量")
        database = get_db()
        po = purchase_order_row(database, purchase_order_id)
        if not po:
            raise ApiError("采购订单不存在", 404)
        timestamp = app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        try:
            cursor = database.execute(
                """
                INSERT INTO inbound_receipts(
                    purchase_order_id, quantity, receiver, receiver_user_id,
                    received_at, sync_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    purchase_order_id,
                    quantity,
                    current_actor_name("系统管理员"),
                    current_actor_id(),
                    timestamp,
                    timestamp,
                ),
            )
            record_audit_event(
                database,
                "INBOUND_RECEIVED",
                "INBOUND_RECEIPT",
                str(cursor.lastrowid),
                related_object_code=po["po_no"],
                payload={"purchaseOrderId": purchase_order_id, "quantity": quantity},
                occurred_at=timestamp,
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        return success(inbound_receipt_data(inbound_receipt_row(database, cursor.lastrowid)), 201)

    @app.get("/api/inbound-receipts")
    def list_inbound_receipts():
        # Operations needs read access in order to push receipts created by the
        # warehouse. Mutating receipt creation remains warehouse-only.
        rows = get_db().execute(
            """
            SELECT ir.*, po.po_no, po.sync_status AS purchase_order_sync_status,
                   po.lingxing_po_id, s.name AS supplier_name,
                   pt.part_code, pt.name AS part_name,
                   pm.name AS product_model_name
            FROM inbound_receipts ir
            JOIN purchase_orders po ON po.id = ir.purchase_order_id
            LEFT JOIN suppliers s ON s.id = po.supplier_id
            LEFT JOIN part_types pt ON pt.id = po.part_type_id
            LEFT JOIN product_models pm ON pm.id = po.product_model_id
            ORDER BY ir.received_at DESC, ir.id DESC
            """
        ).fetchall()
        return success([inbound_receipt_data(row) for row in rows])

    @app.get("/api/inbound-receipts/<int:receipt_id>")
    def get_inbound_receipt(receipt_id: int):
        row = inbound_receipt_row(get_db(), receipt_id)
        if not row:
            raise ApiError("入库收货记录不存在", 404)
        return success(inbound_receipt_data(row))

    @app.post("/api/inbound-receipts/<int:receipt_id>/push")
    def push_inbound_receipt(receipt_id: int):
        require_operations()
        database = get_db()
        row = inbound_receipt_row(database, receipt_id)
        if not row:
            raise ApiError("入库收货记录不存在", 404)
        if row["purchase_order_sync_status"] != "PUSHED":
            raise ApiError("请先成功推送采购订单", 409)
        if row["sync_status"] == "PUSHED":
            return success({**inbound_receipt_data(row), "message": "该入库收货已推送"})
        service = lingxing_service(database)
        ensure_lingxing_operation_ready(service, "inbound_receipt")
        database.execute("BEGIN IMMEDIATE")
        try:
            current = database.execute(
                "SELECT sync_status, push_in_progress, push_started_at "
                "FROM inbound_receipts WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if current["sync_status"] == "PUSHED":
                database.rollback()
                return success({**inbound_receipt_data(inbound_receipt_row(database, receipt_id)), "message": "该入库收货已推送"})
            guard_started_at = app.config["NOW_PROVIDER"]()
            if current["push_in_progress"] and not guard_is_stale(
                current["push_started_at"], guard_started_at, PUSH_GUARD_TIMEOUT
            ):
                raise ApiError("入库收货推送正在进行中", 409)
            # Stamping the start lets a later attempt recover an abandoned guard.
            database.execute(
                "UPDATE inbound_receipts SET push_in_progress = 1, push_started_at = ? "
                "WHERE id = ?",
                (guard_started_at, receipt_id),
            )
            database.commit()
        except Exception:
            database.rollback()
            raise

        try:
            response = service.push(
                "inbound_receipt",
                {
                    "localId": row["id"],
                    "purchaseOrderId": row["lingxing_po_id"],
                    "quantity": row["quantity"],
                },
            )
            lingxing_id = external_identifier(response, "inboundId", "receiptId", "id")
            if not lingxing_id:
                raise LingxingError("领星入库响应缺少对象标识")
            timestamp = app.config["NOW_PROVIDER"]()
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE inbound_receipts
                SET sync_status = 'PUSHED', push_in_progress = 0,
                    lingxing_inbound_id = ?, lingxing_raw_response = ?,
                    push_error = '', pushed_at = ?
                WHERE id = ?
                """,
                (lingxing_id, json.dumps(response, ensure_ascii=False), timestamp, receipt_id),
            )
            record_audit_event(
                database,
                "INBOUND_PUSHED",
                "INBOUND_RECEIPT",
                str(receipt_id),
                related_object_code=row["po_no"],
                payload={"result": "PUSHED", "lingxingInboundId": lingxing_id},
                occurred_at=timestamp,
            )
            database.commit()
        except Exception as error:
            database.rollback()
            message = error.message if isinstance(error, LingxingError) else str(error)
            timestamp = app.config["NOW_PROVIDER"]()
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE inbound_receipts
                SET sync_status = 'FAILED', push_in_progress = 0, push_error = ?
                WHERE id = ?
                """,
                (message, receipt_id),
            )
            record_audit_event(
                database,
                "INBOUND_PUSH_FAILED",
                "INBOUND_RECEIPT",
                str(receipt_id),
                related_object_code=row["po_no"],
                reason=message,
                payload={"result": "FAILED", "error": message},
                occurred_at=timestamp,
            )
            database.commit()
            if isinstance(error, (ApiError, LingxingError)):
                raise
            raise LingxingError(f"领星入库推送失败：{message}") from error
        return success({**inbound_receipt_data(inbound_receipt_row(database, receipt_id)), "message": "入库收货已推送"})

    @app.get("/api/inbound-receipts/<int:receipt_id>/sync-status")
    def inbound_receipt_sync_status(receipt_id: int):
        require_operations()
        row = inbound_receipt_row(get_db(), receipt_id)
        if not row:
            raise ApiError("入库收货记录不存在", 404)
        return success(
            {
                "id": row["id"],
                "syncStatus": row["sync_status"],
                "lingxingId": row["lingxing_inbound_id"] or None,
                "pushedAt": row["pushed_at"],
                "pushError": row["push_error"],
            }
        )

    def production_order_row(database: sqlite3.Connection, production_order_id: int):
        return database.execute(
            """
            SELECT pro.*, po.po_no, po.quantity, po.part_type_id,
                   pb.batch_code, pb.product_model_id, pb.planned_quantity,
                   pb.prefix, pb.generated_at,
                   pm.model_code, pm.name AS product_name
            FROM production_orders pro
            JOIN purchase_orders po ON po.id = pro.purchase_order_id
            JOIN production_batches pb ON pb.id = pro.production_batch_id
            JOIN product_models pm ON pm.id = pb.product_model_id
            WHERE pro.id = ?
            """,
            (production_order_id,),
        ).fetchone()

    def production_order_data(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "purchaseOrderId": row["purchase_order_id"],
            "poNo": row["po_no"],
            "productionBatchId": row["production_batch_id"],
            "productionQrCode": row["batch_code"],
            "identificationCode": batch_identification_code(row["batch_code"]),
            "productModelId": row["product_model_id"],
            "productModelCode": row["model_code"],
            "productName": row["product_name"],
            "quantity": row["planned_quantity"],
            "isExternal": bool(row["is_external"]) if "is_external" in row.keys() else False,
            "createdBy": row["created_by"],
            "createdByUserId": row["created_by_user_id"],
            "createdAt": row["created_at"],
            "downloadUrl": f"/api/production-orders/{row['id']}/qr",
            "products": [
                {
                    "productModelId": row["product_model_id"],
                    "modelCode": row["model_code"],
                    "name": row["product_name"],
                    "quantity": row["planned_quantity"],
                }
            ],
        }

    def stock_in_block_reason(quality_status: str | None, gate: bool) -> str | None:
        """Why 扫码枪入库 must be refused for a batch, or ``None`` when allowed.

        Closes the gap where the 批次质量处理 step had no effect on the flow: a
        暂扣 (HOLD) batch must never enter finished-goods stock. When the
        质量放行 setting is on, the batch additionally has to be registered and
        released (PASSED) first — the same intent the legacy per-unit flow had.
        ``quality_status`` is ``None`` for a batch that was never registered.
        """
        if quality_status == "HOLD":
            return "该批次已暂扣，解除暂扣后才能入库"
        if gate:
            if quality_status is None:
                return "已开启质量放行：请先完成批次登记并放行后再入库"
            if quality_status != "PASSED":
                return "已开启质量放行：该批次需质量合格放行后才能入库"
        return None

    def production_order_progress_map(
        database: sqlite3.Connection, orders: list[sqlite3.Row]
    ) -> dict[int, dict[str, Any]]:
        """Flow state per production order: 登记 → 质量 → 入库.

        Resolves the batch registration/quality status and the cumulative
        received quantity for every order in a fixed number of queries, so the
        warehouse can see which step each order is on (and why a stock-in is
        refused) instead of discovering it only on scan.
        """
        if not orders:
            return {}
        batch_ids = [row["production_batch_id"] for row in orders]
        order_ids = [row["id"] for row in orders]
        registrations = {
            r["production_batch_id"]: r
            for r in database.execute(
                f"""
                SELECT production_batch_id, registered_quantity, quality_status
                FROM batch_trace_records
                WHERE production_batch_id IN ({",".join("?" for _ in batch_ids)})
                """,
                batch_ids,
            ).fetchall()
        }
        received = {
            r["production_order_id"]: r["total"] or 0
            for r in database.execute(
                f"""
                SELECT production_order_id, SUM(quantity) AS total
                FROM inbound_scan_records
                WHERE production_order_id IN ({",".join("?" for _ in order_ids)})
                GROUP BY production_order_id
                """,
                order_ids,
            ).fetchall()
        }
        gate = require_quality_release(database)
        progress: dict[int, dict[str, Any]] = {}
        for row in orders:
            registration = registrations.get(row["production_batch_id"])
            quality_status = registration["quality_status"] if registration else None
            planned = row["planned_quantity"] or 0
            received_quantity = received.get(row["id"], 0)
            blocked = stock_in_block_reason(quality_status, gate)
            progress[row["id"]] = {
                "registered": registration is not None,
                "registeredQuantity": (
                    registration["registered_quantity"] if registration else None
                ),
                "qualityStatus": quality_status,
                "receivedQuantity": received_quantity,
                "remainingQuantity": max(planned - received_quantity, 0),
                "fullyReceived": planned > 0 and received_quantity >= planned,
                "qualityReleaseRequired": gate,
                "canStockIn": blocked is None,
                "stockInBlockedReason": blocked,
            }
        return progress

    def purchase_order_planned_quantity(po: sqlite3.Row) -> int:
        """Resolve how many units a purchase order plans to produce.

        Free-form orders keep their quantity in the template field 实际采购量
        rather than the legacy ``quantity`` column, so fall back to it (and
        finally to 1) instead of inserting NULL into ``planned_quantity``.
        """
        candidates: list[Any] = []
        keys = po.keys()
        if "quantity" in keys:
            candidates.append(po["quantity"])
        if "fields_json" in keys:
            try:
                fields = json.loads(po["fields_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                fields = {}
            if isinstance(fields, dict):
                candidates.append(fields.get("实际采购量"))
        for candidate in candidates:
            if candidate in (None, ""):
                continue
            try:
                quantity = int(float(str(candidate).strip()))
            except (TypeError, ValueError):
                continue
            if 1 <= quantity <= 999999:
                return quantity
        return 1

    def product_model_for_purchase_order(database: sqlite3.Connection, po: sqlite3.Row):
        # Prefer the explicit product link when present; fall back to resolving
        # the product via the legacy 商品(part) -> active trace plan mapping.
        keys = po.keys()
        product_model_id = po["product_model_id"] if "product_model_id" in keys else None
        if product_model_id is not None:
            # A BOM (trace plan) is optional: a product may be a complete,
            # indivisible finished good. The plan is only carried onto the batch
            # when it exists, so component-less products still get a production
            # order and its unique QR code.
            row = database.execute(
                """
                SELECT pm.*, (
                    SELECT tp.id FROM trace_plans tp
                    WHERE tp.product_model_id = pm.id AND tp.status = 'ACTIVE'
                    ORDER BY tp.activated_at DESC, tp.id DESC LIMIT 1
                ) AS trace_plan_id
                FROM product_models pm
                WHERE pm.id = ? AND pm.active = 1
                """,
                (product_model_id,),
            ).fetchone()
            if not row:
                raise ApiError("采购订单关联的产品不存在或已停用", 409)
            return row
        if po["part_type_id"] is None:
            raise ApiError("采购订单未关联可生产的产品", 409)
        rows = database.execute(
            """
            SELECT DISTINCT pm.*, tp.id AS trace_plan_id
            FROM trace_plan_slots slot
            JOIN trace_plans tp ON tp.id = slot.trace_plan_id AND tp.status = 'ACTIVE'
            JOIN product_models pm ON pm.id = tp.product_model_id AND pm.active = 1
            WHERE slot.part_type_id = ?
            ORDER BY tp.activated_at DESC, tp.id DESC
            """,
            (po["part_type_id"],),
        ).fetchall()
        if not rows:
            raise ApiError("采购商品尚未关联可生产的产品型号与生产计划", 409)
        if len({int(row["id"]) for row in rows}) > 1:
            raise ApiError("采购商品关联了多个产品型号，请先明确产品生产计划", 409)
        return rows[0]

    def generate_production_order_for_po(
        database: sqlite3.Connection,
        po: sqlite3.Row,
        *,
        external: bool,
        timestamp: str,
    ) -> int:
        """Create a production batch + order (minting the batch QR) for one PO.

        Resolves the linked product (BOM optional), mints a unique batch code and
        inserts the batch and the production order in a single ``BEGIN IMMEDIATE``
        transaction. ``external`` marks the order as 外采 (externally procured) —
        the trace code is still minted so downstream inbound / stock stays
        uniform. Returns the new production order id. The caller is responsible
        for the "already generated" pre-check; a concurrent duplicate still
        raises a 409 via the ``purchase_order_id`` unique constraint.
        """
        product = product_model_for_purchase_order(database, po)
        require_product_model_access(product["id"])
        planned_quantity = purchase_order_planned_quantity(po)
        try:
            date_code = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y%m%d")
        except ValueError:
            date_code = datetime.now().astimezone().strftime("%Y%m%d")
        prefix = normalize_entity_code(product["serial_prefix"] or product["model_code"], "生产二维码前缀")
        batch_code = new_batch_code(date_code, prefix)
        database.execute("BEGIN IMMEDIATE")
        try:
            batch_cursor = database.execute(
                """
                INSERT INTO production_batches(
                    batch_code, product_model_id, trace_plan_id, prefix,
                    planned_quantity, generated_by, generated_by_user_id, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_code,
                    product["id"],
                    product["trace_plan_id"],
                    prefix,
                    planned_quantity,
                    current_actor_name("系统管理员"),
                    current_actor_id(),
                    timestamp,
                ),
            )
            order_cursor = database.execute(
                """
                INSERT INTO production_orders(
                    purchase_order_id, production_batch_id, is_external,
                    created_by, created_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    po["id"],
                    batch_cursor.lastrowid,
                    1 if external else 0,
                    current_actor_name("系统管理员"),
                    current_actor_id(),
                    timestamp,
                ),
            )
            record_audit_event(
                database,
                "PRODUCTION_ORDER_CREATED",
                "PRODUCTION_ORDER",
                str(order_cursor.lastrowid),
                related_object_code=po["po_no"],
                payload={
                    "purchaseOrderId": po["id"],
                    "productionBatchId": batch_cursor.lastrowid,
                    "batchCode": batch_code,
                    "productModelId": product["id"],
                    "quantity": planned_quantity,
                    "isExternal": bool(external),
                },
                occurred_at=timestamp,
            )
            database.commit()
        except sqlite3.IntegrityError as error:
            database.rollback()
            if "production_orders.purchase_order_id" in str(error):
                raise ApiError("该采购订单已生成生产订单", 409) from error
            raise
        except Exception:
            database.rollback()
            raise
        return order_cursor.lastrowid

    @app.post("/api/production-orders")
    def create_production_order():
        require_admin_or_warehouse()
        payload = request.get_json(silent=True) or {}
        purchase_order_id = business_id(payload.get("purchaseOrderId"), "采购订单")
        external = bool(payload.get("external"))
        database = get_db()
        po = purchase_order_row(database, purchase_order_id)
        if not po:
            raise ApiError("采购订单不存在", 404)
        existing = database.execute(
            "SELECT id FROM production_orders WHERE purchase_order_id = ?",
            (purchase_order_id,),
        ).fetchone()
        if existing:
            raise ApiError("该采购订单已生成生产订单", 409)
        timestamp = app.config["NOW_PROVIDER"]()
        order_id = generate_production_order_for_po(
            database, po, external=external, timestamp=timestamp
        )
        return success(production_order_data(production_order_row(database, order_id)), 201)

    @app.post("/api/production-orders/batch")
    def create_production_orders_batch():
        # Warehouse batch-generates production orders (each minting its trace/QR
        # code) from the purchase orders operations submitted. Each PO is handled
        # in its own transaction so one failure does not abort the rest; orders
        # that already exist are reported as skipped rather than erroring.
        require_admin_or_warehouse()
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("purchaseOrderIds")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ApiError("请选择至少一个采购订单")
        external = bool(payload.get("external"))
        database = get_db()
        timestamp = app.config["NOW_PROVIDER"]()
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in raw_ids:
            try:
                po_id = business_id(raw, "采购订单")
            except ApiError as error:
                failed.append({"purchaseOrderId": raw, "message": error.message})
                continue
            if po_id in seen:
                continue
            seen.add(po_id)
            po = purchase_order_row(database, po_id)
            if not po:
                failed.append({"purchaseOrderId": po_id, "message": "采购订单不存在"})
                continue
            existing = database.execute(
                "SELECT id FROM production_orders WHERE purchase_order_id = ?",
                (po_id,),
            ).fetchone()
            if existing:
                skipped.append(
                    {
                        "purchaseOrderId": po_id,
                        "poNo": po["po_no"],
                        "productionOrderId": existing["id"],
                    }
                )
                continue
            try:
                order_id = generate_production_order_for_po(
                    database, po, external=external, timestamp=timestamp
                )
            except ApiError as error:
                failed.append(
                    {"purchaseOrderId": po_id, "poNo": po["po_no"], "message": error.message}
                )
                continue
            created.append(
                production_order_data(production_order_row(database, order_id))
            )
        return success(
            {"created": created, "skipped": skipped, "failed": failed}, 201
        )

    @app.get("/api/production-orders")
    def list_production_orders():
        require_admin_or_warehouse()
        clauses: list[str] = []
        parameters: list[Any] = []
        po_value = request.args.get("purchaseOrderId")
        if po_value not in {None, ""}:
            clauses.append("pro.purchase_order_id = ?")
            parameters.append(business_id(po_value, "采购订单"))
        operator_id = current_operator_id()
        if operator_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM user_product_model_permissions upp "
                "WHERE upp.user_id = ? AND upp.product_model_id = pb.product_model_id)"
            )
            parameters.append(operator_id)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        database = get_db()
        rows = database.execute(
            f"""
            SELECT pro.*, po.po_no, po.quantity, po.part_type_id,
                   pb.batch_code, pb.product_model_id, pb.planned_quantity,
                   pb.prefix, pb.generated_at, pm.model_code, pm.name AS product_name
            FROM production_orders pro
            JOIN purchase_orders po ON po.id = pro.purchase_order_id
            JOIN production_batches pb ON pb.id = pro.production_batch_id
            JOIN product_models pm ON pm.id = pb.product_model_id
            {where_clause}
            ORDER BY pro.created_at DESC, pro.id DESC
            """,
            parameters,
        ).fetchall()
        # Batched flow state (登记/质量/已入库) so the list shows where each order
        # stands without an extra query per row.
        progress = production_order_progress_map(database, list(rows))
        return success(
            [
                {**production_order_data(row), "progress": progress.get(row["id"], {})}
                for row in rows
            ]
        )

    @app.get("/api/production-orders/<int:production_order_id>")
    def get_production_order(production_order_id: int):
        require_admin_or_warehouse()
        row = production_order_row(get_db(), production_order_id)
        if not row:
            raise ApiError("生产订单不存在", 404)
        require_product_model_access(row["product_model_id"])
        return success(production_order_data(row))

    @app.get("/api/production-orders/<int:production_order_id>/qr")
    def production_order_qr(production_order_id: int):
        require_admin_or_warehouse()
        row = production_order_row(get_db(), production_order_id)
        if not row:
            raise ApiError("生产订单不存在", 404)
        require_product_model_access(row["product_model_id"])
        return Response(
            make_qr_svg(batch_identification_code(row["batch_code"])),
            mimetype="image/svg+xml",
        )

    @app.post("/api/scan-gun/lookup")
    def scan_gun_lookup():
        require_admin_or_warehouse()
        payload = request.get_json(silent=True) or {}
        try:
            batch_code = parse_batch_payload(payload.get("code"))
        except ValueError as error:
            raise ApiError("生产二维码无效或对应生产订单不存在", 404) from error
        database = get_db()
        row = database.execute(
            """
            SELECT pro.*, po.po_no, po.quantity, po.part_type_id,
                   pb.batch_code, pb.product_model_id, pb.planned_quantity,
                   pb.prefix, pb.generated_at, pm.model_code, pm.name AS product_name
            FROM production_orders pro
            JOIN purchase_orders po ON po.id = pro.purchase_order_id
            JOIN production_batches pb ON pb.id = pro.production_batch_id
            JOIN product_models pm ON pm.id = pb.product_model_id
            WHERE pb.batch_code = ? COLLATE NOCASE
            """,
            (batch_code,),
        ).fetchone()
        if not row:
            raise ApiError("生产二维码无效或对应生产订单不存在", 404)
        require_product_model_access(row["product_model_id"])
        # Carry the flow state so the operator sees the batch's 登记/质量 status and
        # how much is already received *before* confirming the stock-in.
        progress = production_order_progress_map(database, [row]).get(row["id"], {})
        return success({**production_order_data(row), "progress": progress})

    @app.get("/api/inbound-scan-records")
    def list_inbound_scan_records():
        # 成品扫码入库 history. Written by /api/scan-gun/inbound; the scan-gun page
        # lists it so the operator can confirm what was just received.
        require_admin_or_warehouse()
        database = get_db()
        try:
            limit = int(request.args.get("limit") or 50)
        except (TypeError, ValueError) as error:
            raise ApiError("查询条数无效") from error
        limit = min(max(limit, 1), 200)
        clauses: list[str] = []
        parameters: list[Any] = []
        order_value = request.args.get("productionOrderId")
        if order_value not in {None, ""}:
            clauses.append("isr.production_order_id = ?")
            parameters.append(business_id(order_value, "生产订单"))
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = database.execute(
            f"""
            SELECT isr.*, pm.model_code, pm.name AS product_name,
                   pb.batch_code, po.po_no,
                   ps.on_hand
            FROM inbound_scan_records isr
            JOIN product_models pm ON pm.id = isr.product_model_id
            LEFT JOIN production_orders pro ON pro.id = isr.production_order_id
            LEFT JOIN production_batches pb ON pb.id = pro.production_batch_id
            LEFT JOIN purchase_orders po ON po.id = pro.purchase_order_id
            LEFT JOIN product_stock ps ON ps.product_model_id = isr.product_model_id
            {where_clause}
            ORDER BY isr.received_at DESC, isr.id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return success(
            [
                {
                    "id": row["id"],
                    "productionOrderId": row["production_order_id"],
                    "productionQrCode": row["batch_code"],
                    "poNo": row["po_no"],
                    "productModelId": row["product_model_id"],
                    "productModelCode": row["model_code"],
                    "productName": row["product_name"],
                    "quantity": row["quantity"],
                    "operatorName": row["operator_name"],
                    "operatorUserId": row["operator_user_id"],
                    "receivedAt": row["received_at"],
                    "onHand": row["on_hand"],
                }
                for row in rows
            ]
        )

    @app.post("/api/scan-gun/inbound")
    def scan_gun_inbound():
        require_admin_or_warehouse()
        payload = request.get_json(silent=True) or {}
        production_order_id = business_id(payload.get("productionOrderId"), "生产订单")
        quantity = business_quantity(payload.get("quantity"), "入库数量")
        database = get_db()
        order = production_order_row(database, production_order_id)
        if not order:
            raise ApiError("生产二维码无效或对应生产订单不存在", 404)
        require_product_model_access(order["product_model_id"])
        # 质量门禁: a 暂扣 batch never enters stock, and when 质量放行 is enabled the
        # batch must be registered and released first (Requirement: the quality
        # step has to actually gate the flow).
        registration = production_batch_registration(database, order["production_batch_id"])
        gate_reason = stock_in_block_reason(
            registration.get("qualityStatus") if registration.get("registered") else None,
            require_quality_release(database),
        )
        if gate_reason:
            raise ApiError(gate_reason, 409)
        timestamp = app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        try:
            cursor = database.execute(
                """
                INSERT INTO inbound_scan_records(
                    production_order_id, product_model_id, quantity,
                    operator_name, operator_user_id, received_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    production_order_id,
                    order["product_model_id"],
                    quantity,
                    current_actor_name("系统管理员"),
                    current_actor_id(),
                    timestamp,
                ),
            )
            database.execute(
                """
                INSERT INTO product_stock(product_model_id, on_hand, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(product_model_id) DO UPDATE SET
                    on_hand = product_stock.on_hand + excluded.on_hand,
                    updated_at = excluded.updated_at
                """,
                (order["product_model_id"], quantity, timestamp),
            )
            latest = database.execute(
                "SELECT on_hand FROM product_stock WHERE product_model_id = ?",
                (order["product_model_id"],),
            ).fetchone()["on_hand"]
            record_audit_event(
                database,
                "FINISHED_GOODS_RECEIVED",
                "PRODUCTION_ORDER",
                str(production_order_id),
                related_object_code=order["batch_code"],
                payload={
                    "inboundRecordId": cursor.lastrowid,
                    "productModelId": order["product_model_id"],
                    "quantity": quantity,
                    "onHand": latest,
                },
                occurred_at=timestamp,
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        return success(
            {
                "id": cursor.lastrowid,
                "productionOrderId": production_order_id,
                "productModelId": order["product_model_id"],
                "quantity": quantity,
                "onHand": latest,
                "receivedAt": timestamp,
            },
            201,
        )

    def purchase_order_export_values(row: sqlite3.Row) -> tuple[dict[str, Any], bool]:
        """Build the exact template column -> value map for one purchase order.

        Values come from what operations entered; legacy supplier/part-linked
        orders still derive their classic defaults so historical exports are
        unchanged. Returns ``(values, is_legacy_linked)`` and never raises, so
        the batch export can include every order.
        """
        try:
            stored_fields = json.loads(row["fields_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            stored_fields = {}
        if not isinstance(stored_fields, dict):
            stored_fields = {}

        values: dict[str, Any] = {column: "" for column in PURCHASE_ORDER_EXPORT_COLUMNS}
        values["标识号"] = row["id"]
        values["采购单号"] = row["po_no"]
        values["采购方"] = "聚星同创仓库管理系统"
        if row["created_by"]:
            values["采购员"] = row["created_by"]

        is_legacy_linked = row["part_type_id"] is not None or row["supplier_id"] is not None
        if is_legacy_linked:
            for key, derived in {
                "供应商": row["supplier_name"],
                "含税": "是",
                "费用分配方式": "按数量",
                "采购币种": "CNY",
                "采购仓库": "默认仓库",
                "SKU": row["part_code"],
                "实际采购量": row["quantity"],
                "含税单价": Decimal("1.00"),
                "联系人": row["supplier_contact"],
                "联系方式": row["supplier_phone"],
                "产品备注": row["part_name"],
            }.items():
                if derived not in (None, ""):
                    values[key] = derived
        if row["product_model_name"] and not values.get("产品备注"):
            values["产品备注"] = row["product_model_name"]

        # Operator-entered template values take precedence over derived defaults.
        for column, value in stored_fields.items():
            if column not in values or value in (None, ""):
                continue
            if column in PURCHASE_ORDER_NUMERIC_COLUMNS:
                number = coerce_export_number(value)
                if number is not None:
                    value = number
            if column == "含税单价" and isinstance(value, (int, float)) and not isinstance(value, bool):
                value = Decimal(str(value))
            values[column] = value
        return values, is_legacy_linked

    @app.get("/api/purchase-orders/export")
    def export_purchase_orders():
        # Batch export: one workbook with every visible order as a row (operations
        # see only their own). Uses the same filters as the list and is lenient so
        # a single legacy order can never fail the whole file.
        require_operations()
        rows = query_purchase_orders()
        table = [
            [
                purchase_order_export_values(row)[0][column]
                for column in PURCHASE_ORDER_EXPORT_COLUMNS
            ]
            for row in rows
        ]
        workbook = build_traceability_xlsx(PURCHASE_ORDER_EXPORT_COLUMNS, table)
        filename = f"采购订单_批量_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            BytesIO(workbook),
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/purchase-orders/<int:purchase_order_id>/export")
    def export_purchase_order(purchase_order_id: int):
        require_operations()
        row = purchase_order_row(get_db(), purchase_order_id)
        if not row:
            raise ApiError("采购订单不存在", 404)
        values, is_legacy_linked = purchase_order_export_values(row)

        # Legacy supplier/part-linked orders still enforce their required columns
        # so the historical export contract (and its rejection path) is preserved.
        if is_legacy_linked:
            required = [
                "标识号", "供应商", "含税", "费用分配方式", "采购币种",
                "采购仓库", "SKU", "实际采购量", "含税单价",
            ]
            missing = [name for name in required if values.get(name) in {None, ""}]
            if missing:
                raise ApiError(f"采购单缺少必填列：{', '.join(missing)}")

        workbook = build_traceability_xlsx(
            PURCHASE_ORDER_EXPORT_COLUMNS,
            [[values[column] for column in PURCHASE_ORDER_EXPORT_COLUMNS]],
        )
        return send_file(
            BytesIO(workbook),
            as_attachment=True,
            download_name=f"采购订单_{safe_archive_name(row['po_no'])}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def upsert_app_setting(
        database: sqlite3.Connection,
        key: str,
        value: object,
        timestamp: str,
    ) -> None:
        database.execute(
            """
            INSERT INTO app_settings(
                setting_key, setting_value, is_secret, updated_by,
                updated_by_user_id, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_by = excluded.updated_by,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = excluded.updated_at
            """,
            (
                key,
                str(value),
                current_actor_name("系统管理员"),
                current_actor_id(),
                timestamp,
            ),
        )

    # A sync that dies mid-flight (process restart, container kill, power loss)
    # used to leave ``inventory_sync.in_progress`` at "1" forever, permanently
    # blocking every later sync with 409 and requiring manual DB surgery. A guard
    # older than this window is treated as abandoned so the next request recovers.
    INVENTORY_SYNC_GUARD_TIMEOUT = timedelta(minutes=15)
    # Same reasoning for the per-record 领星 push guards (采购单 / 供应收货): a crash
    # between taking ``push_in_progress`` and clearing it would otherwise block that
    # single record from ever being pushed again.
    PUSH_GUARD_TIMEOUT = timedelta(minutes=15)

    def guard_is_stale(started_at: object, now: str, timeout: timedelta) -> bool:
        """Whether an in-progress guard set at ``started_at`` may be taken over."""
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            # Without a usable start time we cannot prove the guard was abandoned,
            # so keep blocking: losing concurrency protection is worse than a
            # delayed retry. Guards stranded by a dead process are cleared
            # deterministically at startup (see clear_abandoned_guards in db.py).
            return False
        if (started.tzinfo is None) != (current.tzinfo is None):
            started = started.replace(tzinfo=None)
            current = current.replace(tzinfo=None)
        return current - started >= timeout

    def inventory_sync_settings(database: sqlite3.Connection) -> dict[str, str]:
        rows = database.execute(
            "SELECT setting_key, setting_value FROM app_settings "
            "WHERE setting_key LIKE 'inventory_sync.%'"
        ).fetchall()
        return {row["setting_key"].split(".", 1)[1]: row["setting_value"] for row in rows}

    def inventory_sync_items(database: sqlite3.Connection) -> list[dict[str, Any]]:
        """Per-product stock lines joined to their last Lingxing sync status.

        A product with no ``product_stock_sync`` row has never been synced, so it
        reports ``PENDING`` (design: inventory sync shows the per-product state so
        operations can push a selected subset or everything).
        """
        rows = database.execute(
            """
            SELECT ps.product_model_id, ps.on_hand, ps.updated_at,
                   pm.model_code, pm.name,
                   pss.sync_status, pss.synced_quantity, pss.lingxing_id,
                   pss.push_error, pss.synced_at
            FROM product_stock ps
            JOIN product_models pm ON pm.id = ps.product_model_id
            LEFT JOIN product_stock_sync pss
                ON pss.product_model_id = ps.product_model_id
            ORDER BY pm.model_code
            """
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "productModelId": row["product_model_id"],
                    "sku": row["model_code"],
                    "productName": row["name"],
                    "quantity": row["on_hand"],
                    "stockUpdatedAt": row["updated_at"],
                    "syncStatus": row["sync_status"] or "PENDING",
                    "syncedQuantity": row["synced_quantity"] if row["synced_quantity"] is not None else 0,
                    "lingxingId": row["lingxing_id"] or None,
                    "pushError": row["push_error"] or "",
                    "syncedAt": row["synced_at"],
                }
            )
        return items

    def inventory_sync_overall(
        database: sqlite3.Connection, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        settings = inventory_sync_settings(database)
        return {
            "syncStatus": settings.get("status") or "PENDING",
            "syncedAt": settings.get("synced_at") or None,
            "lingxingId": settings.get("lingxing_id") or None,
            "error": settings.get("error") or "",
            "inProgress": settings.get("in_progress") == "1",
            "total": len(items),
            "pushed": sum(1 for item in items if item["syncStatus"] == "PUSHED"),
            "pending": sum(1 for item in items if item["syncStatus"] == "PENDING"),
            "failed": sum(1 for item in items if item["syncStatus"] == "FAILED"),
        }

    def mark_product_stock_synced(
        database: sqlite3.Connection,
        product_model_id: int,
        synced_quantity: int,
        lingxing_id: str,
        timestamp: str,
    ) -> None:
        database.execute(
            """
            INSERT INTO product_stock_sync(
                product_model_id, sync_status, synced_quantity, lingxing_id,
                push_error, synced_at
            ) VALUES (?, 'PUSHED', ?, ?, '', ?)
            ON CONFLICT(product_model_id) DO UPDATE SET
                sync_status = 'PUSHED',
                synced_quantity = excluded.synced_quantity,
                lingxing_id = excluded.lingxing_id,
                push_error = '',
                synced_at = excluded.synced_at
            """,
            (product_model_id, synced_quantity, lingxing_id, timestamp),
        )

    def mark_product_stock_sync_failed(
        database: sqlite3.Connection,
        product_model_id: int,
        message: str,
        timestamp: str,
    ) -> None:
        # Preserve the last successful synced_quantity / lingxing_id on conflict;
        # only the status, error and time are refreshed for a failed push.
        database.execute(
            """
            INSERT INTO product_stock_sync(
                product_model_id, sync_status, synced_quantity, lingxing_id,
                push_error, synced_at
            ) VALUES (?, 'FAILED', 0, '', ?, ?)
            ON CONFLICT(product_model_id) DO UPDATE SET
                sync_status = 'FAILED',
                push_error = excluded.push_error,
                synced_at = excluded.synced_at
            """,
            (product_model_id, message, timestamp),
        )

    @app.get("/api/inventory-sync")
    def get_inventory_sync():
        # Per-product sync board: current stock plus each product's last Lingxing
        # sync status/time, and an overall summary. Drives the operations
        # inventory-sync page (全部同步 / 同步所选).
        require_operations()
        database = get_db()
        items = inventory_sync_items(database)
        return success(
            {"overall": inventory_sync_overall(database, items), "items": items}
        )

    def fetch_pending_receipts_by_sku(
        service: Any, scope_skus: set[str] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Resolve pending Lingxing 收货单 (采购收货单) grouped by product SKU.

        Calls 查询收货单列表 (getOrderList) for 待收货 purchase 收货单 and returns a
        map of ``SKU -> [{"order_sn", "item_id", "good_num"}]`` so 库存同步 can
        fast-receive each product's line. ``good_num`` is the still-outstanding
        quantity (通知收货量 − 已收货量). ``getOrderList`` cannot filter by SKU, so
        when ``scope_skus`` is given only those SKUs are kept and pagination stops
        as soon as every scoped SKU is matched — a subset sync no longer scans all
        pending 收货单. Paginates up to a safe cap otherwise.
        """
        by_sku: dict[str, list[dict[str, Any]]] = defaultdict(list)
        offset = 0
        length = 200
        for _ in range(10):  # cap at 2000 收货单 rows per sync
            response = service.push(
                "receipt_list",
                {
                    "date_type": 3,
                    "order_type": 1,
                    "status": 10,
                    "offset": offset,
                    "length": length,
                },
            )
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            rows = data.get("list") or []
            for order in rows:
                order_sn = str(order.get("order_sn") or "").strip()
                if not order_sn:
                    continue
                for item in order.get("item_list") or []:
                    sku = str(item.get("sku") or "").strip()
                    if not sku:
                        continue
                    sku_upper = sku.upper()
                    if scope_skus is not None and sku_upper not in scope_skus:
                        continue
                    try:
                        item_id = int(item.get("item_id"))
                    except (TypeError, ValueError):
                        continue
                    try:
                        notice = int(float(item.get("notice_num_total") or 0))
                        received = int(float(item.get("product_receive_num") or 0))
                    except (TypeError, ValueError):
                        notice, received = 0, 0
                    good_num = notice - received
                    if good_num <= 0:
                        continue
                    by_sku[sku_upper].append(
                        {"order_sn": order_sn, "item_id": item_id, "good_num": good_num}
                    )
            # Stop early once every scoped SKU has been resolved.
            if scope_skus is not None and scope_skus.issubset(by_sku.keys()):
                break
            total = int(data.get("total") or 0)
            offset += length
            if offset >= total or not rows:
                break
        return by_sku

    def fast_receive_product(
        service: Any,
        model_code: str,
        by_sku: dict[str, list[dict[str, Any]]],
    ) -> tuple[list[str], int]:
        """Fast-receive every pending 收货单 line for one product's SKU.

        Groups the matched lines by 收货单号 and calls 快捷入库 (fastReceive) once
        per 收货单 with its ``item_list``. Returns the received 收货单号 list and the
        total 良品量. Raises ``ApiError`` when no pending 收货单 matches the SKU.
        """
        lines = by_sku.get(str(model_code or "").upper(), [])
        if not lines:
            raise ApiError("未找到该产品待收货的领星收货单", 404)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for line in lines:
            grouped[line["order_sn"]].append(line)
        order_sns: list[str] = []
        good_total = 0
        for order_sn, items in grouped.items():
            service.push(
                "inventory_sync",
                {
                    "order_sn": order_sn,
                    "item_list": [
                        {
                            "id": item["item_id"],
                            "product_good_num": item["good_num"],
                            "product_bad_num": 0,
                        }
                        for item in items
                    ],
                },
            )
            order_sns.append(order_sn)
            good_total += sum(item["good_num"] for item in items)
        return order_sns, good_total

    @app.post("/api/inventory-sync")
    def sync_inventory_to_lingxing():
        # 库存同步 receives each product's goods into Lingxing through 快捷入库
        # (fastReceive): it resolves the product's pending 收货单 via 查询收货单列表
        # (getOrderList, matched by SKU) and then fast-receives every outstanding
        # line. Per-product PUSHED/FAILED status is recorded so operations sees
        # the corresponding order sync; a subset can be synced via
        # ``productModelIds`` and the whole run is guarded against concurrency.
        require_operations()
        database = get_db()
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("productModelIds")
        selected_ids: list[int] | None = None
        if raw_ids not in (None, ""):
            if not isinstance(raw_ids, list):
                raise ApiError("productModelIds 必须是数组")
            parsed = [business_id(raw, "产品") for raw in raw_ids]
            selected_ids = list(dict.fromkeys(parsed))
            if not selected_ids:
                raise ApiError("请选择至少一个产品")
        service = lingxing_service(database)
        # Both the 收货单 lookup and the fast-receive must be reachable.
        ensure_lingxing_operation_ready(service, "receipt_list")
        ensure_lingxing_operation_ready(service, "inventory_sync")

        # Resolve the stock rows in scope (a selected subset or every product)
        # before acquiring the in-progress guard so a bad selection fails fast.
        base_query = (
            "SELECT ps.product_model_id, ps.on_hand, pm.model_code, pm.name "
            "FROM product_stock ps JOIN product_models pm ON pm.id = ps.product_model_id"
        )
        if selected_ids is None:
            stock_rows = database.execute(base_query + " ORDER BY pm.model_code").fetchall()
        else:
            placeholders = ",".join("?" for _ in selected_ids)
            stock_rows = database.execute(
                base_query
                + f" WHERE ps.product_model_id IN ({placeholders}) ORDER BY pm.model_code",
                selected_ids,
            ).fetchall()
            found = {row["product_model_id"] for row in stock_rows}
            missing = [pid for pid in selected_ids if pid not in found]
            if missing:
                raise ApiError("所选产品没有库存记录，无法同步", 404)

        scope_ids = [row["product_model_id"] for row in stock_rows]
        timestamp = app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        try:
            guard = database.execute(
                "SELECT setting_value, updated_at FROM app_settings "
                "WHERE setting_key = 'inventory_sync.in_progress'"
            ).fetchone()
            recovered_stale_guard = False
            if guard and guard["setting_value"] == "1":
                if not guard_is_stale(
                    guard["updated_at"], timestamp, INVENTORY_SYNC_GUARD_TIMEOUT
                ):
                    raise ApiError("库存同步正在进行中", 409)
                # The previous run never finished; take the guard over and record it.
                recovered_stale_guard = True
            upsert_app_setting(database, "inventory_sync.in_progress", "1", timestamp)
            if recovered_stale_guard:
                record_audit_event(
                    database,
                    "INVENTORY_SYNC_GUARD_RECOVERED",
                    "INVENTORY_SYNC",
                    "PRODUCT_STOCK",
                    reason="上次库存同步未正常结束，已自动接管",
                    payload={"staleSince": guard["updated_at"]},
                    occurred_at=timestamp,
                )
            database.commit()
        except Exception:
            database.rollback()
            raise

        def release_guard(status: str, error_message: str, results: list[dict[str, Any]]) -> None:
            finished_at = app.config["NOW_PROVIDER"]()
            database.execute("BEGIN IMMEDIATE")
            for key, value in {
                "inventory_sync.in_progress": "0",
                "inventory_sync.status": status,
                "inventory_sync.synced_at": finished_at,
                "inventory_sync.error": error_message,
            }.items():
                upsert_app_setting(database, key, value, finished_at)
            event_type = "INVENTORY_SYNC_PUSHED" if status == "PUSHED" else "INVENTORY_SYNC_FAILED"
            record_audit_event(
                database,
                event_type,
                "INVENTORY_SYNC",
                "PRODUCT_STOCK",
                reason=error_message,
                payload={
                    "result": status,
                    "scope": "SELECTED" if selected_ids is not None else "ALL",
                    "productModelIds": scope_ids,
                    "results": results,
                },
                occurred_at=finished_at,
            )
            database.commit()

        # Resolve the pending 收货单 once (scoped to the products being synced so a
        # subset sync doesn't scan every pending 收货单), then fast-receive each line.
        scope_skus = {
            str(row["model_code"]).upper() for row in stock_rows if row["model_code"]
        }
        try:
            by_sku = fetch_pending_receipts_by_sku(service, scope_skus)
        except Exception as error:
            message = error.message if isinstance(error, (ApiError, LingxingError)) else str(error)
            finished_at = app.config["NOW_PROVIDER"]()
            database.execute("BEGIN IMMEDIATE")
            for product_model_id in scope_ids:
                mark_product_stock_sync_failed(database, product_model_id, message, finished_at)
            database.commit()
            release_guard("FAILED", message, [])
            if isinstance(error, (ApiError, LingxingError)):
                raise
            raise LingxingError(f"领星收货单查询失败：{message}") from error

        results: list[dict[str, Any]] = []
        succeeded = 0
        finished_at = app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        try:
            for row in stock_rows:
                try:
                    order_sns, good_total = fast_receive_product(
                        service, row["model_code"], by_sku
                    )
                    mark_product_stock_synced(
                        database,
                        row["product_model_id"],
                        good_total,
                        ",".join(order_sns),
                        finished_at,
                    )
                    succeeded += 1
                    results.append(
                        {
                            "productModelId": row["product_model_id"],
                            "status": "PUSHED",
                            "orderSns": order_sns,
                            "goodNum": good_total,
                        }
                    )
                except (ApiError, LingxingError) as error:
                    mark_product_stock_sync_failed(
                        database, row["product_model_id"], error.message, finished_at
                    )
                    results.append(
                        {
                            "productModelId": row["product_model_id"],
                            "status": "FAILED",
                            "message": error.message,
                        }
                    )
            database.commit()
        except Exception:
            database.rollback()
            release_guard("FAILED", "库存同步异常", results)
            raise

        failed = [item for item in results if item["status"] == "FAILED"]
        if succeeded == 0:
            # Nothing was received (every scoped product failed) — surface the
            # first reason and leave local stock untouched.
            message = failed[0]["message"] if failed else "没有可同步的收货单"
            release_guard("FAILED", message, results)
            raise LingxingError(f"领星快捷入库失败：{message}")
        overall_status = "PUSHED" if not failed else "PARTIAL"
        release_guard(
            overall_status,
            failed[0]["message"] if failed else "",
            results,
        )
        items = inventory_sync_items(database)
        return success(
            {
                "syncStatus": overall_status,
                "syncedAt": finished_at,
                "itemCount": succeeded,
                "results": results,
                "overall": inventory_sync_overall(database, items),
                "items": items,
            }
        )

    @app.get("/api/purchase-orders/<int:purchase_order_id>/factory-progress")
    def purchase_order_factory_progress(purchase_order_id: int):
        require_operations()
        database = get_db()
        po = purchase_order_row(database, purchase_order_id)
        if not po:
            raise ApiError("采购订单不存在", 404)
        row = database.execute(
            """
            SELECT pro.id AS production_order_id,
                   COALESCE(SUM(isr.quantity), 0) AS latest_quantity
            FROM production_orders pro
            LEFT JOIN inbound_scan_records isr ON isr.production_order_id = pro.id
            WHERE pro.purchase_order_id = ?
            GROUP BY pro.id
            """,
            (purchase_order_id,),
        ).fetchone()
        return success(
            {
                "purchaseOrderId": purchase_order_id,
                "productionOrderGenerated": bool(row),
                "productionOrderId": row["production_order_id"] if row else None,
                "latestQuantity": int(row["latest_quantity"]) if row else 0,
            }
        )

    return app


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("TRACE_HOST", "0.0.0.0")
    port = int(os.environ.get("TRACE_PORT", "5080"))
    print(f"聚星同创仓库管理系统已启动：http://127.0.0.1:{port}")
    application.run(host=host, port=port, threaded=True, debug=False)
