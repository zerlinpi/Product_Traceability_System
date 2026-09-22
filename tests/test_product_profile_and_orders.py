"""Component-less production orders, purchase-order summaries and product profiles.

These cover the three behaviours added on top of the batch-traceability feature:

- a product may be a complete, indivisible finished good (no BOM / trace plan)
  and still get a production order with a unique QR code, taking its planned
  quantity from the purchase-order template when the legacy ``quantity`` column
  is unset;
- every purchase order exposes a flattened ``summary`` block (identity, dates,
  ordered vs received quantity and amount) so the receiving / production pages
  show consistent data;
- product management persists the extended product profile, filling the
  system-managed columns (SKU / 品名 / 创建人 / 创建时间 / 更新时间 / 状态 /
  识别码) itself and ignoring client-supplied values for them.
"""
from __future__ import annotations

import app as app_module
from capability_helpers import bootstrap_admin, make_auth_app
from traceability.codes import parse_batch_payload
from traceability.db import connect_database


def send(client, csrf, path, body, method="post"):
    return getattr(client, method)(path, json=body, headers={"X-CSRF-Token": csrf})


def created(client, csrf, path, body, method="post"):
    response = send(client, csrf, path, body, method)
    assert response.status_code in (200, 201), (path, response.status_code, response.get_json())
    return response.get_json()["data"]


def simple_product(client, csrf, name="完整成品", **extra):
    """Register a product with no components (no BOM, hence no trace plan)."""
    return created(client, csrf, "/api/products", {"name": name, "components": [], **extra})


def free_form_order(client, csrf, product, fields):
    return created(
        client,
        csrf,
        "/api/purchase-orders",
        {"productModelId": product["id"], "fields": fields},
    )


# --------------------------------------------------------------------------- #
# Component-less production orders
# --------------------------------------------------------------------------- #
def test_component_less_product_can_generate_a_production_order(tmp_path):
    """A finished good without a BOM still produces one batch and one QR code."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(admin, csrf, "完整成品甲")
    assert product["componentCount"] == 0
    assert product["tracePlanId"] is None

    order = free_form_order(admin, csrf, product, {"SKU": "SKU-A", "实际采购量": 120})
    # Free-form orders keep the quantity in the template, not the legacy column.
    assert order["quantity"] is None

    production = created(admin, csrf, "/api/production-orders", {"purchaseOrderId": order["id"]})
    assert production["purchaseOrderId"] == order["id"]
    assert production["quantity"] == 120
    assert parse_batch_payload(production["identificationCode"]) == production["productionQrCode"]
    qr = admin.get(production["downloadUrl"])
    assert qr.status_code == 200
    assert qr.mimetype == "image/svg+xml"

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT trace_plan_id, planned_quantity FROM production_batches"
        ).fetchone()
        # No BOM means no plan is attached, and the quantity came from the template.
        assert row["trace_plan_id"] is None
        assert row["planned_quantity"] == 120
    finally:
        database.close()


def test_production_order_quantity_falls_back_when_template_has_no_quantity(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(admin, csrf, "完整成品乙")
    order = free_form_order(admin, csrf, product, {"SKU": "SKU-B"})
    production = created(admin, csrf, "/api/production-orders", {"purchaseOrderId": order["id"]})
    assert production["quantity"] == 1


def test_production_order_rejects_a_deactivated_product(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(admin, csrf, "停用成品")
    order = free_form_order(admin, csrf, product, {"实际采购量": 5})
    assert send(admin, csrf, f"/api/products/{product['id']}", {"active": False}, "put").status_code == 200

    rejected = send(admin, csrf, "/api/production-orders", {"purchaseOrderId": order["id"]})
    assert rejected.status_code == 409
    assert "不存在或已停用" in rejected.get_json()["message"]
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 0
    finally:
        database.close()


# --------------------------------------------------------------------------- #
# Purchase-order summary block
# --------------------------------------------------------------------------- #
def test_purchase_order_summary_reports_ordered_and_received_values(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(admin, csrf, "跑步机 A1")
    order = free_form_order(
        admin,
        csrf,
        product,
        {
            "SKU": "SKU-777",
            "店铺": "美国旗舰店",
            "FNSKU": "X00ABCDEF",
            "含税单价": 35.5,
            "实际采购量": 120,
            "预计到货时间": "2026-08-01",
        },
    )

    before = order["summary"]
    assert before["sku"] == "SKU-777"
    assert before["productName"] == "跑步机 A1"
    assert before["shop"] == "美国旗舰店"
    assert before["fnsku"] == "X00ABCDEF"
    assert before["owner"] == "系统管理员"
    assert before["orderedAt"] == order["createdAt"]
    assert before["plannedArrivalAt"] == "2026-08-01"
    assert before["unitPrice"] == 35.5
    assert before["orderedQuantity"] == 120
    # Nothing received yet.
    assert before["actualArrivalAt"] is None
    assert before["receivedQuantity"] == 0
    assert before["orderAmount"] == 4260.0
    assert before["receivedAmount"] == 0

    created(admin, csrf, "/api/inbound-receipts", {"purchaseOrderId": order["id"], "quantity": 70})
    created(admin, csrf, "/api/inbound-receipts", {"purchaseOrderId": order["id"], "quantity": 30})

    after = admin.get(f"/api/purchase-orders/{order['id']}").get_json()["data"]["summary"]
    # Receipts accumulate, and the amount follows the received quantity.
    assert after["receivedQuantity"] == 100
    assert after["receivedAmount"] == 3550.0
    assert after["actualArrivalAt"]
    assert after["orderedQuantity"] == 120
    assert after["orderAmount"] == 4260.0

    # The list endpoint carries the same block.
    listed = next(
        item for item in admin.get("/api/purchase-orders").get_json()["data"]
        if item["id"] == order["id"]
    )
    assert listed["summary"] == after


def test_purchase_order_summary_without_price_reports_no_amount(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(admin, csrf, "无报价产品")
    order = free_form_order(admin, csrf, product, {"实际采购量": 10})
    summary = order["summary"]
    assert summary["unitPrice"] is None
    assert summary["orderAmount"] is None
    assert summary["receivedAmount"] is None
    assert summary["orderedQuantity"] == 10


# --------------------------------------------------------------------------- #
# Extended product profile
# --------------------------------------------------------------------------- #
def test_product_profile_is_stored_with_system_managed_columns(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = created(
        admin,
        csrf,
        "/api/products",
        {
            "name": "跑步机 A1",
            "components": [],
            "attributes": {
                "品牌": "聚星",
                "单价": 199.5,
                "报关HSCODE": "9506911900",
                # Client-supplied values for system columns must be ignored.
                "创建人": "伪造用户",
                "SKU": "伪造SKU",
                "状态": "停用",
                # Unknown columns are dropped.
                "不存在的列": "x",
            },
        },
    )
    attributes = product["attributes"]
    assert attributes["品牌"] == "聚星"
    assert attributes["单价"] == 199.5
    assert attributes["报关HSCODE"] == "9506911900"
    assert attributes["SKU"] == product["productCode"]
    assert attributes["识别码"] == product["productCode"]
    assert attributes["品名"] == "跑步机 A1"
    assert attributes["创建人"] == "系统管理员"
    assert attributes["状态"] == "启用"
    assert attributes["创建时间"] and attributes["更新时间"]
    assert "不存在的列" not in attributes


def test_product_profile_partial_update_keeps_stored_columns(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(
        admin, csrf, "跑步机 A1", attributes={"品牌": "聚星", "采购员": "张三"}
    )
    original = product["attributes"]

    updated = created(
        admin,
        csrf,
        f"/api/products/{product['id']}",
        {"name": "跑步机 A1 Pro", "attributes": {"品牌类型": "自主品牌"}},
        method="put",
    )
    merged = updated["attributes"]
    # Untouched columns survive; the new one is added.
    assert merged["品牌"] == "聚星"
    assert merged["采购员"] == "张三"
    assert merged["品牌类型"] == "自主品牌"
    # 品名 tracks the product, 创建时间/创建人 are preserved.
    assert merged["品名"] == "跑步机 A1 Pro"
    assert merged["创建时间"] == original["创建时间"]
    assert merged["创建人"] == original["创建人"]


def test_product_profile_status_follows_the_product_and_rejects_bad_payloads(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = simple_product(admin, csrf, "状态跟随产品")
    disabled = created(
        admin, csrf, f"/api/products/{product['id']}", {"active": False}, method="put"
    )
    assert disabled["attributes"]["状态"] == "停用"

    rejected = send(admin, csrf, "/api/products", {"name": "坏数据", "attributes": ["x"]})
    assert rejected.status_code == 400
    assert "产品资料" in rejected.get_json()["message"]

    too_long = send(
        admin,
        csrf,
        "/api/products",
        {"name": "超长字段", "attributes": {"报价备注": "x" * 501}},
    )
    assert too_long.status_code == 400


def test_product_attribute_columns_endpoint_lists_the_template(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    meta = admin.get("/api/product-attribute-columns").get_json()["data"]
    assert meta["columns"] == app_module.PRODUCT_ATTRIBUTE_COLUMNS
    assert set(meta["derived"]) == app_module.PRODUCT_ATTRIBUTE_DERIVED_COLUMNS
    # Every derived column is part of the template itself.
    assert set(meta["columns"]) >= app_module.PRODUCT_ATTRIBUTE_DERIVED_COLUMNS
    for column in ("SKU", "品名", "供应商名称", "报关HSCODE", "巴西发票默认原产地"):
        assert column in meta["columns"]
