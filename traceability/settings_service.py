"""System settings: the Lingxing integration block.

The settings page saves the Lingxing credentials and the three write-endpoint
routes, and the push buttons elsewhere in the UI need to know which of those
routes are configured. Both sides are built here:

* ``clean_lingxing_endpoint`` validates an endpoint before it is stored. The
  write endpoints are configured in settings (route path or full URL) so
  operations can enable the push features without redeploying, which is exactly
  why an absolute URL has to pass the outbound endpoint policy.
* ``lingxing_endpoint_readiness`` reports, per write operation, whether an
  endpoint is configured, and ``lingxing_settings_block`` is the masked payload
  the settings page renders.

The 质量放行 switch on the same page lives in ``quality.py``, because the
stock-in gate reads it too.

Extracted from ``app.py`` when the settings routes moved to
``traceability/api/settings.py``. ``app.py`` still re-exports
``clean_lingxing_endpoint`` because tests import it from there.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from flask import current_app

from traceability.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyError,
    assert_url_allowed,
)
from traceability.errors import ApiError
from traceability.lingxing import load_credentials
from traceability.lingxing_writes import merged_lingxing_endpoints

__all__ = [
    "clean_lingxing_endpoint",
    "lingxing_endpoint_readiness",
    "lingxing_settings_block",
]


def clean_lingxing_endpoint(value: object, label: str) -> str:
    """Validate a Lingxing endpoint value: empty, a ``/path`` or an allowlisted URL.

    A relative path is the preferred form: it is joined onto the configured base
    URL, so it cannot change which host the server contacts. An absolute URL has
    to satisfy the endpoint policy — see ``traceability/endpoint_policy.py`` for
    why an unrestricted URL here is a server-side request forgery primitive.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 300:
        raise ApiError(f"{label}不能超过 300 个字符")
    if not re.fullmatch(r"(?:https?://[^\s]+|/[^\s]*)", text):
        raise ApiError(f"{label}需为以 / 开头的路径或 http(s) 链接")
    if "://" in text:
        policy = current_app.config.get("LINGXING_ENDPOINT_POLICY") or EndpointPolicy()
        try:
            assert_url_allowed(text, policy, label=label)
        except EndpointPolicyError as error:
            raise ApiError(str(error)) from error
    return text


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
