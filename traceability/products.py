"""Product catalog: the extended product profile and the configuration payload.

Two things describe a product beyond its row in ``product_models``:

* The extended profile: the columns of the external product template, stored as
  JSON in ``product_models.attributes_json``. The column list is a contract with
  that template, and the system-derived columns are always overwritten from the
  product record so the profile cannot contradict it.
* The configuration payload the product pages render: the product, its active
  BOM (``trace_plans.py``) and how much has been generated from it.
  ``list_product_configurations`` builds the same payload for many products in
  a bounded number of queries.

Extracted from ``app.py`` when the product routes moved to
``traceability/api/products.py``. ``app.py`` still re-exports
``PRODUCT_ATTRIBUTE_COLUMNS`` and ``PRODUCT_ATTRIBUTE_DERIVED_COLUMNS`` because
tests import them from there.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

from traceability.errors import ApiError
from traceability.serializers import _trace_plan_slot_dict
from traceability.trace_plans import _TRACE_PLAN_SLOT_SELECT, trace_plan_slots

__all__ = [
    "PRODUCT_ATTRIBUTE_COLUMNS",
    "PRODUCT_ATTRIBUTE_COLUMN_SET",
    "PRODUCT_ATTRIBUTE_DERIVED_COLUMNS",
    "PRODUCT_IMAGE_COLUMNS",
    "PRODUCT_IMAGE_EXTENSIONS",
    "PRODUCT_IMAGE_MAX_BYTES",
    "PRODUCT_IMAGE_MAX_PIXELS",
    "PRODUCT_IMAGE_MAX_SIDE",
    "PRODUCT_IMAGE_NAME_PATTERN",
    "clean_product_attributes",
    "cleared_product_attribute_columns",
    "list_product_configurations",
    "product_attributes_with_defaults",
    "product_configuration_dict",
]


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
# Pixel limits read from the image header. A few-KB PNG can declare a
# 50000×50000 canvas and exhaust the memory of every browser that renders the
# product card, so the byte cap alone is not enough. 10000 px is the longest
# side marketplaces such as Amazon accept for a main picture; 40 MP still
# covers any phone or studio photo that fits in 5 MB.
PRODUCT_IMAGE_MAX_SIDE = 10_000
PRODUCT_IMAGE_MAX_PIXELS = 40_000_000
# Accepted picture formats, keyed by the file signature so a renamed executable
# can never be stored (the browser-supplied name and type are not trusted).
PRODUCT_IMAGE_EXTENSIONS = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
PRODUCT_IMAGE_NAME_PATTERN = re.compile(r"^[0-9a-f]{8,}-[0-9a-f]{16,}\.(?:png|jpg|gif|webp)$")

# Filled in by the system from the product record itself, so the form never asks
# for them and a client cannot desynchronize them from the real product.
PRODUCT_ATTRIBUTE_DERIVED_COLUMNS = {
    "SKU", "品名", "创建时间", "创建人", "更新时间", "状态", "识别码",
}


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
        if isinstance(value, (bool, int, float)):
            attributes[column] = value
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) > 500:
            raise ApiError(f"字段“{column}”内容不能超过 500 个字符")
        attributes[column] = text
    return attributes


def cleared_product_attribute_columns(raw_attributes: object) -> set[str]:
    """Return the editable columns a product update explicitly blanks.

    ``PUT /api/products/<id>`` merges the submitted profile over the stored one,
    so a column the client leaves out keeps its value. A column sent as ``null``
    or as whitespace-only text is the client saying "clear this field", and
    ``clean_product_attributes`` alone would silently drop it. Unknown and
    system-derived columns are ignored here too, so a client can never blank
    SKU / 品名 / 状态.
    """
    if not isinstance(raw_attributes, dict):
        return set()
    cleared: set[str] = set()
    for key, value in raw_attributes.items():
        column = str(key).strip()
        if column not in PRODUCT_ATTRIBUTE_COLUMN_SET:
            continue
        if column in PRODUCT_ATTRIBUTE_DERIVED_COLUMNS:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            cleared.add(column)
    return cleared


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
