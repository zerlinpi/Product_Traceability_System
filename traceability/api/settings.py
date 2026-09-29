"""System settings — HTTP layer.

``GET/PUT /api/settings`` is the administrator's settings page: the 质量放行
switch plus the Lingxing credentials and write-endpoint routes.
``GET /api/lingxing/status`` answers the narrower question the push buttons ask
(is Lingxing configured, and which write endpoints are set) without exposing a
secret, so operations can call it too.

Validation and the masked read-back live in ``traceability/settings_service.py``.
The writes stay in the route: saving settings is one transaction that also
records the audit event and, when the credentials change, drops the cached
Lingxing token.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import (
    current_actor_id,
    current_actor_name,
    require_admin,
    require_operations,
)
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.lingxing import load_credentials, mask_secret
from traceability.lingxing_writes import (
    LINGXING_ENDPOINT_SETTING_KEYS,
    LINGXING_TOKEN_CACHE,
)
from traceability.quality import require_quality_release
from traceability.responses import success
from traceability.settings_service import (
    clean_lingxing_endpoint,
    lingxing_endpoint_readiness,
    lingxing_settings_block,
)
from traceability.validators import now_iso, parse_bool

settings_bp = Blueprint("settings", __name__)


@settings_bp.get("/api/lingxing/status")
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


@settings_bp.get("/api/settings")
def get_settings():
    require_admin()
    database = get_db()
    return success(
        {
            "requireQualityRelease": require_quality_release(database),
            "lingxing": lingxing_settings_block(database),
        }
    )


@settings_bp.put("/api/settings")
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
    quality_release_submitted = "requireQualityRelease" in payload
    database.execute("BEGIN IMMEDIATE")
    try:
        # Read the current gate inside the write lock so the comparison (and an
        # omitted field) can never act on a value another admin just changed.
        current_quality_release = require_quality_release(database)
        quality_release = parse_bool(
            payload.get("requireQualityRelease"), current_quality_release
        )
        # Flipping the gate mid-flow would change the rules for parts an open
        # station already accepted, so only an actual change is refused while
        # a station is scanning. Re-sending the current value (for example when
        # saving the 领星 credentials from the same page) is always allowed.
        if quality_release != current_quality_release:
            active = database.execute(
                "SELECT COUNT(*) AS n FROM scan_sessions WHERE machine_id IS NOT NULL"
            ).fetchone()["n"]
            if active:
                raise ApiError("仍有工位正在扫码，请先在对应工位清空当前流程", 409)
        if quality_release_submitted:
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
            timestamp = current_app.config["NOW_PROVIDER"]()
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
            endpoint_timestamp = current_app.config["NOW_PROVIDER"]()
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
