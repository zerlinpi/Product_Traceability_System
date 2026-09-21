from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from traceability.lingxing import (
    LingxingError,
    LingxingIntegrationService,
    mask_secret,
    sanitize_response,
)


class CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("缺失凭据时不应访问网络")


# Feature: batch-traceability, Property 35: 凭据缺失时安全降级且零外部调用
@settings(max_examples=100, deadline=None)
@given(
    missing_key=st.sampled_from(["appId", "appSecret"]),
    missing_value=st.sampled_from([None, "", " ", "\t\n"]),
)
def test_missing_credentials_fail_before_external_call(missing_key, missing_value):
    config = {"appId": "app-1", "appSecret": "secret-1"}
    config[missing_key] = missing_value
    client = CountingClient()
    service = LingxingIntegrationService(
        http_client=client,
        credential_config=config,
        endpoints={"token": "mock://token", "purchase_order": "mock://po"},
    )

    with pytest.raises(LingxingError, match="领星凭据未配置"):
        service.push("purchase_order", {"poNo": "PO-1"})
    assert client.calls == 0


def test_missing_business_endpoint_fails_before_token_network_call():
    client = CountingClient()
    service = LingxingIntegrationService(
        http_client=client,
        credential_config={"appId": "1234567890abcdef", "appSecret": "secret-1"},
        endpoints={"token": "mock://token"},
    )

    with pytest.raises(LingxingError, match="领星接口地址未配置"):
        service.push("purchase_order", {"poNo": "PO-1"})
    assert client.calls == 0


# Feature: batch-traceability, Property 36: 密钥脱敏
@settings(max_examples=100, deadline=None)
@given(st.text(min_size=0, max_size=64))
def test_secret_mask_never_exposes_more_than_four_trailing_characters(value):
    masked = mask_secret(value)
    assert len(masked) == len(value)
    if len(value) <= 4:
        assert masked == "*" * len(value)
    else:
        assert masked.endswith(value[-4:])
        assert masked[:-4] == "*" * (len(value) - 4)
        # An input that is already in exactly the masked representation is
        # safely idempotent; every other raw value must visibly change.
        if value[:-4] != "*" * (len(value) - 4):
            assert masked != value

    sanitized = sanitize_response(
        {
            "appSecret": value,
            "access_token": value,
            "accessToken": value,
            "refresh_token": value,
            "refreshToken": value,
        }
    )
    assert set(sanitized.values()) == {masked}
