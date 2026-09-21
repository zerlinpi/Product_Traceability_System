from capability_helpers import (
    bootstrap_admin,
    create_po,
    insert_user,
    login,
    make_auth_app,
    post,
    seed_scenario,
)
from traceability.db import connect_database


def test_three_role_capability_chain_with_mock_lingxing(tmp_path):
    """运营建采购单 → 仓管生产/扫码入库 → 运营同步库存与查看进度。"""

    app, database_path, fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    scenario = seed_scenario(admin, admin_csrf, "ROLE-E2E")
    insert_user(database_path, "warehouse.e2e", "WAREHOUSE", [scenario["product"]["id"]])
    insert_user(database_path, "operations.e2e", "OPERATIONS")
    warehouse, warehouse_csrf = login(app, "warehouse.e2e")
    operations, operations_csrf = login(app, "operations.e2e")

    purchase_order = create_po(operations, operations_csrf, scenario, 25)
    production = post(
        warehouse,
        warehouse_csrf,
        "/api/production-orders",
        {"purchaseOrderId": purchase_order["id"]},
    )
    lookup = post(
        warehouse,
        warehouse_csrf,
        "/api/scan-gun/lookup",
        {"code": production["identificationCode"]},
        expected=200,
    )
    assert lookup["id"] == production["id"]
    inbound = post(
        warehouse,
        warehouse_csrf,
        "/api/scan-gun/inbound",
        {"productionOrderId": production["id"], "quantity": 18},
    )
    assert inbound["onHand"] == 18

    # 库存同步 resolves the product's pending 收货单 (getOrderList) then fast-receives.
    fake.add_receipt("CR-ROLE-E2E", scenario["product"]["productCode"], 18)
    synced = post(
        operations,
        operations_csrf,
        "/api/inventory-sync",
        {},
        expected=200,
    )
    assert synced["syncStatus"] == "PUSHED"
    progress = operations.get(
        f"/api/purchase-orders/{purchase_order['id']}/factory-progress"
    )
    assert progress.status_code == 200, progress.get_json()
    assert progress.get_json()["data"]["productionOrderGenerated"] is True
    assert progress.get_json()["data"]["latestQuantity"] == 18
    assert [operation for operation, _payload in fake.calls] == [
        "receipt_list",
        "inventory_sync",
    ]

    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0] == 1
        assert database.execute("SELECT on_hand FROM product_stock").fetchone()[0] == 18
    finally:
        database.close()

