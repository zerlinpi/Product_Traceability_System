from __future__ import annotations

import sqlite3

import pytest

from capability_helpers import (
    bootstrap_admin,
    create_po,
    insert_user,
    login,
    make_auth_app,
    post,
    seed_scenario,
)
from traceability.auth import VALID_ROLES
from traceability.db import connect_database


# Feature: batch-traceability, Property 33: 角色互斥不变式（恰好三角色）
def test_role_set_is_exactly_three_and_database_rejects_operator(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    assert {"ADMIN", "WAREHOUSE", "OPERATIONS"} == VALID_ROLES
    database = connect_database(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                """
                INSERT INTO users(username, display_name, password_hash, role, created_at, updated_at)
                VALUES ('old.operator', '旧录入员', 'hash', 'OPERATOR', 'now', 'now')
                """
            )
    finally:
        database.close()
    assert app is not None


# Feature: batch-traceability, Property 59: 新增受限操作的三角色授权矩阵
def test_new_capability_routes_enforce_final_role_matrix_without_side_effects(tmp_path):
    app, database_path, fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    scenario = seed_scenario(admin, admin_csrf, "AUTHZ")
    insert_user(database_path, "warehouse", "WAREHOUSE", [scenario["product"]["id"]])
    insert_user(database_path, "operations", "OPERATIONS")
    warehouse, warehouse_csrf = login(app, "warehouse")
    operations, operations_csrf = login(app, "operations")

    anonymous = app.test_client()
    assert anonymous.post("/api/purchase-orders", json={}).status_code == 401
    assert warehouse.post(
        "/api/purchase-orders",
        json={},
        headers={"X-CSRF-Token": warehouse_csrf},
    ).status_code == 403
    purchase_order = create_po(operations, operations_csrf, scenario)

    assert operations.post(
        "/api/inbound-receipts",
        json={"purchaseOrderId": purchase_order["id"], "quantity": 1},
        headers={"X-CSRF-Token": operations_csrf},
    ).status_code == 403
    post(
        warehouse,
        warehouse_csrf,
        "/api/inbound-receipts",
        {"purchaseOrderId": purchase_order["id"], "quantity": 1},
    )

    assert operations.post(
        "/api/production-orders",
        json={"purchaseOrderId": purchase_order["id"]},
        headers={"X-CSRF-Token": operations_csrf},
    ).status_code == 403
    production = post(
        warehouse,
        warehouse_csrf,
        "/api/production-orders",
        {"purchaseOrderId": purchase_order["id"]},
    )

    assert operations.post(
        "/api/scan-gun/lookup",
        json={"code": production["productionQrCode"]},
        headers={"X-CSRF-Token": operations_csrf},
    ).status_code == 403
    lookup = post(
        warehouse,
        warehouse_csrf,
        "/api/scan-gun/lookup",
        {"code": production["productionQrCode"]},
        expected=200,
    )
    assert lookup["id"] == production["id"]

    assert warehouse.get(f"/api/purchase-orders/{purchase_order['id']}/export").status_code == 403
    assert warehouse.get(f"/api/purchase-orders/{purchase_order['id']}/factory-progress").status_code == 403
    assert warehouse.post(
        "/api/inventory-sync", json={}, headers={"X-CSRF-Token": warehouse_csrf}
    ).status_code == 403
    assert operations.get(f"/api/purchase-orders/{purchase_order['id']}/export").status_code == 200
    assert operations.get(f"/api/purchase-orders/{purchase_order['id']}/factory-progress").status_code == 200

    assert warehouse.get("/api/settings").status_code == 403
    assert operations.get("/api/settings").status_code == 403
    assert admin.get("/api/settings").status_code == 200

    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM inbound_receipts").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM inbound_scan_records").fetchone()[0] == 0
    finally:
        database.close()
    assert fake.calls == []

