"""采购单下单 (``/erp/sc/routing/purchase/purchase/setOrders``) contract.

The real endpoint transitions an EXISTING Lingxing purchase order from 待下单 to
待到货. It is keyed by the Lingxing 采购单号 (``order_sn``), answers with an empty
``data`` list on success, and reports rejections with ``code: 1`` plus per-order
entries such as ``{"order_sn": "...", "detail": "错误：采购单状态已变更"}`` while the
envelope ``message`` stays ``"success"``.

These tests pin that contract at both levels: the adapter (request shape,
success/failure detection, per-order detail surfacing) and the route (what gets
persisted, and that a rejected order is never marked as pushed).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from capability_helpers import bootstrap_admin, make_auth_app
from traceability.db import connect_database
from traceability.lingxing import (
    DEFAULT_PURCHASE_ORDER_PATH,
    LingxingError,
    LingxingIntegrationService,
    LingxingTokenCache,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
SUCCESS_ENVELOPE = {
    "code": 0,
    "message": "success",
    "error_details": [],
    "request_id": "8DFEE1CA-EC9B-F401-5D41-9F95251D5D50",
    "response_time": "2021-11-12 12:41:04",
    "data": [],
    "total": 0,
}


class RecordingClient:
    """Captures the outgoing request and replays a canned envelope."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, *, json_data, headers, timeout):
        self.requests.append({"method": method, "url": url, "body": json_data})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def build_service(responses):
    cache = LingxingTokenCache()
    cache.access_token = "token-abc"
    cache.expires_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    client = RecordingClient(responses)
    service = LingxingIntegrationService(
        http_client=client,
        token_cache=cache,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        # A 16-byte AppID so the AES-ECB signing key is valid.
        credential_config={"appId": "1234567890abcdef", "appSecret": "secret"},
        endpoints={
            "token": "mock://token",
            "refresh_token": "mock://refresh",
            "purchase_order": DEFAULT_PURCHASE_ORDER_PATH,
        },
    )
    return service, client


# --------------------------------------------------------------------------- #
# Adapter level
# --------------------------------------------------------------------------- #
def test_set_orders_posts_the_order_sn_array_to_the_documented_route():
    service, client = build_service([SUCCESS_ENVELOPE])
    response = service.push("purchase_order", {"order_sn": ["PO210705007"]})

    assert response["code"] == 0
    assert len(client.requests) == 1
    sent = client.requests[0]
    assert sent["method"] == "POST"
    # The documented path, with the signed query appended by the adapter.
    assert DEFAULT_PURCHASE_ORDER_PATH in sent["url"]
    assert sent["url"].startswith("https://openapi.lingxing.com")
    for parameter in ("app_key=", "access_token=", "timestamp=", "sign="):
        assert parameter in sent["url"]
    # order_sn travels in the JSON body as an array.
    assert sent["body"] == {"order_sn": ["PO210705007"]}


def test_empty_data_response_is_a_success():
    """`data: []` is the documented success shape and must not be an error."""
    service, _client = build_service([SUCCESS_ENVELOPE])
    assert service.push("purchase_order", {"order_sn": ["PO1"]})["data"] == []


def test_rejected_order_surfaces_the_per_order_detail():
    """code=1 carries the reason only in data[].detail; message stays "success"."""
    failure = {
        "code": 1,
        "message": "success",
        "error_details": [],
        "request_id": "1DE78F69-1370-4C80-F97A-D9DDF0710F9E",
        "response_time": "2021-11-12 12:39:50",
        "data": [{"order_sn": "PO211112002", "detail": "错误：采购单状态已变更"}],
        "total": 0,
    }
    service, _client = build_service([failure])
    with pytest.raises(LingxingError) as raised:
        service.push("purchase_order", {"order_sn": ["PO211112002"]})
    message = raised.value.message
    assert "采购单状态已变更" in message
    assert "PO211112002" in message
    # The useless envelope message must not replace the real reason.
    assert message != "领星接口请求失败（1）：success"
    assert raised.value.code == "1"
    assert raised.value.retryable is False


def test_success_code_with_per_item_rejection_is_still_a_failure():
    """A 0 envelope that rejects an item must not be reported as pushed."""
    partial = {**SUCCESS_ENVELOPE, "data": [{"order_sn": "PO1", "detail": "错误：采购单不存在"}]}
    service, _client = build_service([partial])
    with pytest.raises(LingxingError, match="采购单不存在"):
        service.push("purchase_order", {"order_sn": ["PO1"]})


def test_error_details_entries_are_reported():
    envelope = {**SUCCESS_ENVELOPE, "code": 1, "error_details": ["签名错误"]}
    service, _client = build_service([envelope])
    with pytest.raises(LingxingError, match="签名错误"):
        service.push("purchase_order", {"order_sn": ["PO1"]})


def test_ordinary_payloads_are_not_mistaken_for_failures():
    """A mapping `data` (e.g. the token payload) carries no per-item results."""
    service, _client = build_service([{"code": 0, "data": {"message": "ok", "id": 7}}])
    assert service.push("purchase_order", {"order_sn": ["PO1"]})["data"]["id"] == 7


# --------------------------------------------------------------------------- #
# Route level
# --------------------------------------------------------------------------- #
def prepare_order(tmp_path, fields=None):
    app, database_path, fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    product = admin.post(
        "/api/products",
        json={"name": "完整成品", "components": []},
        headers={"X-CSRF-Token": csrf},
    ).get_json()["data"]
    order = admin.post(
        "/api/purchase-orders",
        json={"productModelId": product["id"], "fields": fields or {"实际采购量": 10}},
        headers={"X-CSRF-Token": csrf},
    ).get_json()["data"]
    return admin, csrf, database_path, fake, order


def test_push_sends_the_order_sn_and_records_it(tmp_path):
    admin, csrf, database_path, fake, order = prepare_order(tmp_path)
    response = admin.post(
        f"/api/purchase-orders/{order['id']}/push", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["syncStatus"] == "PUSHED"
    # Defaults to our own document number, which is what the export writes.
    assert data["lingxingPoId"] == order["poNo"]
    assert fake.calls == [("purchase_order", {"order_sn": [order["poNo"]]})]

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT lingxing_po_id FROM purchase_orders WHERE id = ?", (order["id"],)
        ).fetchone()[0] == order["poNo"]
    finally:
        database.close()


def test_explicit_order_sn_overrides_the_local_document_number(tmp_path):
    admin, csrf, _database_path, fake, order = prepare_order(tmp_path)
    response = admin.post(
        f"/api/purchase-orders/{order['id']}/push",
        json={"orderSn": "PO210705007"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["lingxingPoId"] == "PO210705007"
    assert fake.calls == [("purchase_order", {"order_sn": ["PO210705007"]})]


def test_template_order_number_is_used_when_present(tmp_path):
    admin, csrf, _database_path, fake, order = prepare_order(
        tmp_path, fields={"采购单号": "PO-LX-0001", "实际采购量": 4}
    )
    assert order["poNo"] == "PO-LX-0001"
    admin.post(f"/api/purchase-orders/{order['id']}/push", headers={"X-CSRF-Token": csrf})
    assert fake.calls == [("purchase_order", {"order_sn": ["PO-LX-0001"]})]


def test_oversized_order_sn_is_rejected_without_calling_lingxing(tmp_path):
    admin, csrf, database_path, fake, order = prepare_order(tmp_path)
    response = admin.post(
        f"/api/purchase-orders/{order['id']}/push",
        json={"orderSn": "P" * 65},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "领星采购单号" in response.get_json()["message"]
    assert fake.calls == []

    database = connect_database(database_path)
    try:
        # No external call, so the record stays untouched and not "pushing".
        row = database.execute(
            "SELECT sync_status, push_in_progress, push_error FROM purchase_orders WHERE id = ?",
            (order["id"],),
        ).fetchone()
        assert tuple(row) == ("PENDING", 0, "")
    finally:
        database.close()


def test_purchase_push_is_enabled_by_its_own_endpoint_alone(tmp_path):
    """采购下单 must not wait for the inbound / inventory routes to be set."""
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    status = admin.get("/api/lingxing/status").get_json()["data"]
    # The documented purchase route ships as a default; the others stay opt-in.
    assert status["endpointsConfigured"]["purchaseOrder"] is True
    assert status["endpointsConfigured"]["inboundReceipt"] is False
    assert status["writeEndpointsConfigured"] is False

    settings = admin.get("/api/settings").get_json()["data"]["lingxing"]
    assert settings["endpoints"]["purchaseOrder"] == DEFAULT_PURCHASE_ORDER_PATH
    assert settings["endpointsConfigured"]["purchaseOrder"] is True
