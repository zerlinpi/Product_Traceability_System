from __future__ import annotations

import base64
from copy import deepcopy
import urllib.parse

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from datetime import datetime, timezone

from traceability.lingxing import (
    LingxingError,
    LingxingIntegrationService,
    LingxingTokenCache,
    compute_sign,
)


APP_ID = "1234567890abcdef"


safe_values = st.one_of(
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.text(min_size=0, max_size=40),
    st.booleans(),
)


# Feature: batch-traceability, Property 40: 请求签名确定性与一致携带
@settings(max_examples=100, deadline=None)
@given(
    params=st.dictionaries(
        st.text(alphabet=st.characters(categories=("Ll", "Lu", "Nd")), min_size=1, max_size=12),
        safe_values,
        min_size=1,
        max_size=8,
    ),
    token=st.text(min_size=1, max_size=32),
    timestamp=st.integers(min_value=1, max_value=4_102_444_800).map(str),
)
def test_sign_is_deterministic_and_does_not_mutate_params(params, token, timestamp):
    before = deepcopy(params)
    first = compute_sign(params, token, timestamp, app_id=APP_ID)
    second = compute_sign(params, token, timestamp, app_id=APP_ID)
    assert first == second
    # The sign is raw base64 (the transport URL-encodes it once); it must not be
    # pre-encoded here, so it decodes directly without unquoting.
    assert "%" not in first
    decoded = base64.b64decode(first)
    assert len(decoded) == 48
    assert params == before


# Feature: batch-traceability, Property 41: 签名前置校验缺失即中止
@pytest.mark.parametrize(
    ("params", "token", "timestamp"),
    [({}, "token", "1"), ({"id": 1}, "", "1"), ({"id": 1}, "token", "")],
)
def test_sign_prerequisites_are_required(params, token, timestamp):
    with pytest.raises(LingxingError, match="无法生成请求签名"):
        compute_sign(params, token, timestamp, app_id=APP_ID)


def test_official_signature_vector_is_stable():
    assert compute_sign(
        {"purchaseOrderId": 7, "quantity": 12, "remark": "测试"},
        "cached-token",
        "1784707200",
        app_id=APP_ID,
    ) == "0YT73UIQRx4IYULpGUxpE3ulYtklhrQL8oPEnrsHtQMC3sVlLAIV+Tr2nwCfD21Y"


class CapturingSignStrategy:
    def __init__(self) -> None:
        self.calls = []

    def sign(self, params, access_token, timestamp):
        self.calls.append((deepcopy(dict(params)), access_token, timestamp))
        return "signed-value"


class CapturingHttpClient:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, *, json_data, headers, timeout):
        self.calls.append((method, url, deepcopy(dict(json_data)), dict(headers), timeout))
        return {"ok": True}


def test_push_carries_the_exact_signed_timestamp_and_preserves_source_params():
    timestamp = 1_784_707_200
    payload = {"purchaseOrderId": 7, "quantity": 12, "remark": "测试"}
    before = deepcopy(payload)
    cache = LingxingTokenCache()
    cache.access_token = "cached-token"
    cache.expires_at = datetime.fromtimestamp(timestamp + 3600, timezone.utc)
    strategy = CapturingSignStrategy()
    http = CapturingHttpClient()
    service = LingxingIntegrationService(
        http_client=http,
        token_cache=cache,
        clock=lambda: datetime.fromtimestamp(timestamp, timezone.utc),
        sign_strategy=strategy,
        credential_config={"appId": "app", "appSecret": "secret", "id": "account"},
        endpoints={"purchase_order": "mock://purchase-order"},
    )

    assert service.push("purchase_order", payload) == {"ok": True}
    assert payload == before
    assert strategy.calls == [(before, "cached-token", str(timestamp))]
    method, url, sent, headers, timeout = http.calls[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert method == "POST"
    assert sent == before
    assert query["app_key"] == ["app"]
    assert query["access_token"] == ["cached-token"]
    assert query["timestamp"] == [strategy.calls[0][2]]
    assert query["sign"] == ["signed-value"]
    assert headers == {"Content-Type": "application/json"}
    assert timeout == 30
