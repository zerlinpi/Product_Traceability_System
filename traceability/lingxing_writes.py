"""Shared helpers for the Lingxing write paths.

Purchase orders, inbound receipts and inventory sync all push to Lingxing, and
they share this plumbing: constructing the client, checking that an endpoint is
configured, taking the in-progress guard, and pulling an external id out of a
response. It lived inside ``create_app`` until the purchase-order routes needed
to leave; a blueprint cannot reach a nested function, and copying it per domain
would have meant three copies of the same push protocol.

``traceability/lingxing.py`` remains the API client — HTTP, signing, tokens,
retry. Nothing here re-implements any of that.

Extracted from ``app.py``; the names are re-exported from there so existing
imports keep working.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from flask import current_app

from traceability.lingxing import (
    DEFAULT_API_BASE_URL,
    LingxingIntegrationService,
    LingxingTokenCache,
    RetryConfig,
)

__all__ = [
    "PUSH_GUARD_TIMEOUT",
    "LINGXING_ENDPOINT_SETTING_KEYS",
    "LINGXING_TOKEN_CACHE",
    "ensure_lingxing_operation_ready",
    "external_identifier",
    "guard_is_stale",
    "lingxing_service",
    "load_lingxing_endpoint_overrides",
    "merged_lingxing_endpoints",
]


LINGXING_TOKEN_CACHE = LingxingTokenCache()

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

# How long an in-progress push may sit before another request may take the
# guard over. Shared by every push path (purchase order, inbound receipt): a
# guard stranded by a dead worker must not block the operation forever.
PUSH_GUARD_TIMEOUT = timedelta(minutes=15)


def merged_lingxing_endpoints(database: sqlite3.Connection) -> dict[str, str]:
    endpoints = dict(current_app.config.get("LINGXING_ENDPOINTS") or {})
    endpoints.update(load_lingxing_endpoint_overrides(database))
    return endpoints

def lingxing_service(database: sqlite3.Connection) -> LingxingIntegrationService:
    factory = current_app.config.get("LINGXING_SERVICE_FACTORY")
    if factory:
        return factory(database)
    return LingxingIntegrationService(
        database=database,
        http_client=current_app.config.get("LINGXING_HTTP_CLIENT"),
        clock=current_app.config.get("LINGXING_CLOCK"),
        sleeper=current_app.config.get("LINGXING_SLEEP"),
        retry_config=RetryConfig(
            max_retries=int(current_app.config["LINGXING_MAX_RETRIES"]),
            request_timeout=int(current_app.config["LINGXING_REQUEST_TIMEOUT"]),
            retry_interval=int(current_app.config["LINGXING_RETRY_INTERVAL"]),
        ),
        endpoints=merged_lingxing_endpoints(database),
        credential_config=current_app.config.get("LINGXING_CREDENTIALS"),
        api_base_url=current_app.config.get("LINGXING_API_BASE_URL", DEFAULT_API_BASE_URL),
        policy=current_app.config.get("LINGXING_ENDPOINT_POLICY"),
        token_cache=LINGXING_TOKEN_CACHE,
    )

def ensure_lingxing_operation_ready(service: Any, operation: str) -> None:
    validator = getattr(service, "ensure_operation_ready", None)
    if callable(validator):
        validator(operation)
    else:
        service.credentials()

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
