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
def test_lingxing_role_set_has_exactly_one_of_the_three_final_roles(tmp_path):
    _app, database_path, _fake = make_auth_app(tmp_path)
    assert {"ADMIN", "WAREHOUSE", "OPERATIONS"} == VALID_ROLES

    database = connect_database(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                """
                INSERT INTO users(username, display_name, password_hash, role, created_at, updated_at)
                VALUES ('legacy.operator', '旧录入员', 'hash', 'OPERATOR', 'now', 'now')
                """
            )
    finally:
        database.close()


# Feature: batch-traceability, Property 34: 领星操作的角色授权
def test_lingxing_routes_enforce_operations_and_warehouse_roles_without_side_effects(tmp_path):
    app, database_path, fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    scenario = seed_scenario(admin, admin_csrf, "LX-AUTHZ")
    insert_user(database_path, "lx-warehouse", "WAREHOUSE", [scenario["product"]["id"]])
    insert_user(database_path, "lx-operations", "OPERATIONS")
    warehouse, warehouse_csrf = login(app, "lx-warehouse")
    operations, operations_csrf = login(app, "lx-operations")

    anonymous = app.test_client()
    assert anonymous.post("/api/purchase-orders", json={}).status_code == 401

    denied_po = warehouse.post(
        "/api/purchase-orders",
        json={
            "supplierId": scenario["supplier"]["id"],
            "partTypeId": scenario["part"]["id"],
            "quantity": 5,
        },
        headers={"X-CSRF-Token": warehouse_csrf},
    )
    assert denied_po.status_code == 403
    purchase_order = create_po(operations, operations_csrf, scenario, quantity=5)

    denied_receipt = operations.post(
        "/api/inbound-receipts",
        json={"purchaseOrderId": purchase_order["id"], "quantity": 5},
        headers={"X-CSRF-Token": operations_csrf},
    )
    assert denied_receipt.status_code == 403
    receipt = post(
        warehouse,
        warehouse_csrf,
        "/api/inbound-receipts",
        {"purchaseOrderId": purchase_order["id"], "quantity": 5},
    )

    calls_before = list(fake.calls)
    assert warehouse.post(
        f"/api/purchase-orders/{purchase_order['id']}/push",
        headers={"X-CSRF-Token": warehouse_csrf},
    ).status_code == 403
    assert fake.calls == calls_before

    assert operations.post(
        f"/api/purchase-orders/{purchase_order['id']}/push",
        headers={"X-CSRF-Token": operations_csrf},
    ).status_code == 200
    calls_before = list(fake.calls)
    assert warehouse.post(
        f"/api/inbound-receipts/{receipt['id']}/push",
        headers={"X-CSRF-Token": warehouse_csrf},
    ).status_code == 403
    assert fake.calls == calls_before
    assert operations.post(
        f"/api/inbound-receipts/{receipt['id']}/push",
        headers={"X-CSRF-Token": operations_csrf},
    ).status_code == 200

    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM inbound_receipts").fetchone()[0] == 1
    finally:
        database.close()
    assert [operation for operation, _payload in fake.calls] == [
        "purchase_order",
        "inbound_receipt",
    ]
