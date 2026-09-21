from __future__ import annotations

import json

from app import LINGXING_TOKEN_CACHE
from capability_helpers import bootstrap_admin, make_auth_app
from traceability.db import connect_database


# Feature: batch-traceability, Property 60: 领星凭据设置原子保存与脱敏返回
def test_lingxing_credentials_are_saved_atomically_and_only_masked_in_responses(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    secrets = {"appId": "app-123456", "appSecret": "secret-123456"}
    LINGXING_TOKEN_CACHE.access_token = "stale-token"
    LINGXING_TOKEN_CACHE.refresh_token = "stale-refresh"
    response = admin.put(
        "/api/settings",
        json={"requireQualityRelease": True, **secrets},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.get_json()
    assert LINGXING_TOKEN_CACHE.access_token == ""
    assert LINGXING_TOKEN_CACHE.refresh_token == ""
    rendered = json.dumps(response.get_json(), ensure_ascii=False)
    for value in secrets.values():
        assert value not in rendered

    loaded = admin.get("/api/settings")
    assert loaded.status_code == 200
    data = loaded.get_json()["data"]
    assert data["requireQualityRelease"] is True
    assert data["lingxing"]["configured"] is True
    assert data["lingxing"]["appId"].endswith("3456")
    assert data["lingxing"]["appSecret"].endswith("3456")
    assert all(value not in json.dumps(data, ensure_ascii=False) for value in secrets.values())

    database = connect_database(database_path)
    try:
        rows = database.execute(
            "SELECT setting_key, setting_value, is_secret FROM app_settings ORDER BY setting_key"
        ).fetchall()
        assert len(rows) == 2
        assert all(row["is_secret"] == 1 for row in rows)
        audit = database.execute(
            "SELECT payload_json FROM audit_events WHERE object_type='SYSTEM_SETTING' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert all(value not in audit for value in secrets.values())
    finally:
        database.close()


# Feature: batch-traceability, Property 61: 系统设置校验拒绝且无副作用
def test_partial_or_invalid_credential_update_is_rejected_without_mutation(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    valid = {"appId": "app-1", "appSecret": "secret-1"}
    assert admin.put(
        "/api/settings", json=valid, headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    database = connect_database(database_path)
    try:
        before = [tuple(row) for row in database.execute("SELECT * FROM app_settings ORDER BY setting_key")]
    finally:
        database.close()

    partial = admin.put(
        "/api/settings",
        json={"appSecret": "replacement-only"},
        headers={"X-CSRF-Token": csrf},
    )
    assert partial.status_code == 400
    too_long = admin.put(
        "/api/settings",
        json={"appId": "a" * 257, "appSecret": "s"},
        headers={"X-CSRF-Token": csrf},
    )
    assert too_long.status_code == 400
    database = connect_database(database_path)
    try:
        assert [tuple(row) for row in database.execute("SELECT * FROM app_settings ORDER BY setting_key")] == before
    finally:
        database.close()

