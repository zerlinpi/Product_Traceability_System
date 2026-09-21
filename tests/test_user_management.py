from __future__ import annotations

import pytest

from capability_helpers import bootstrap_admin, make_auth_app, seed_scenario
from traceability.db import connect_database


# Feature: batch-traceability, Property 62: 新增用户校验与恰好单角色
@pytest.mark.parametrize("role", ["ADMIN", "WAREHOUSE", "OPERATIONS"])
def test_admin_creates_each_final_role_and_only_warehouse_keeps_scope(tmp_path, role):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    scenario = seed_scenario(admin, csrf, f"USER-{role}")
    response = admin.post(
        "/api/users",
        json={
            "username": (role[0].lower() + "x" * 49),
            "displayName": f"{role} 用户",
            "password": "Password@123",
            "role": role,
            "productModelIds": [scenario["product"]["id"]],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.get_json()
    user = response.get_json()["data"]
    assert user["role"] == role
    assert user["mustChangePassword"] is True
    assert user["productModelIds"] == (
        [scenario["product"]["id"]] if role == "WAREHOUSE" else []
    )
    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT role FROM users WHERE id=?", (user["id"],)
        ).fetchone()[0] == role
    finally:
        database.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"username": ""},
        {"username": "a" * 51},
        {"username": "含中文"},
        {"password": "short"},
        {"password": "x" * 129},
        {"role": "OPERATOR"},
        {"role": ""},
    ],
)
def test_invalid_user_request_creates_no_account(tmp_path, patch):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    payload = {
        "username": "valid.user",
        "displayName": "有效用户",
        "password": "Password@123",
        "role": "WAREHOUSE",
    }
    payload.update(patch)
    database = connect_database(database_path)
    try:
        before = database.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        database.close()
    response = admin.post(
        "/api/users", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 400
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before
    finally:
        database.close()


# Feature: batch-traceability, Property 63: 重复用户名拒绝
def test_duplicate_username_is_rejected_without_new_row(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    payload = {
        "username": "duplicate.user",
        "displayName": "第一账号",
        "password": "Password@123",
        "role": "OPERATIONS",
    }
    first = admin.post("/api/users", json=payload, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 201
    database = connect_database(database_path)
    try:
        before = database.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        database.close()
    duplicate = admin.post(
        "/api/users",
        json={**payload, "displayName": "第二账号"},
        headers={"X-CSRF-Token": csrf},
    )
    assert duplicate.status_code == 409
    assert "账号已存在" in duplicate.get_json()["message"]
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before
    finally:
        database.close()

