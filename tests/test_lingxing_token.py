from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from traceability.lingxing import (
    LingxingError,
    LingxingIntegrationService,
    LingxingTokenCache,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


class QueueClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def request(self, method, url, *, json_data, headers, timeout):
        self.calls.append((url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def service_with(client, cache):
    return LingxingIntegrationService(
        http_client=client,
        token_cache=cache,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        credential_config={"appId": "app", "appSecret": "secret"},
        endpoints={"token": "mock://token", "refresh_token": "mock://refresh"},
    )


# Feature: batch-traceability, Property 37: 访问令牌缓存有效性判定
@settings(max_examples=100, deadline=None)
@given(remaining=st.integers(min_value=1, max_value=180))
def test_access_token_cache_uses_strict_sixty_second_margin(remaining):
    cache = LingxingTokenCache()
    cache.access_token = "cached"
    cache.expires_at = NOW + timedelta(seconds=remaining)
    client = QueueClient([{"access_token": "fresh", "expires_in": 3600}])
    service = service_with(client, cache)

    token = service.access_token()
    if remaining > 60:
        assert token == "cached"
        assert client.calls == []
    else:
        assert token == "fresh"
        assert client.calls == [("mock://token?appId=app&appSecret=secret", 10)]


# Feature: batch-traceability, Property 38: 令牌刷新与回退路径
def test_refresh_failure_falls_back_to_new_token_and_updates_cache():
    cache = LingxingTokenCache()
    cache.access_token = "expired"
    cache.expires_at = NOW
    cache.refresh_token = "refresh-1"
    cache.refresh_expires_at = NOW + timedelta(hours=1)
    client = QueueClient(
        [
            LingxingError("refresh rejected", retryable=False),
            {"data": {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}},
        ]
    )
    service = service_with(client, cache)

    assert service.access_token() == "new-access"
    assert cache.refresh_token == "new-refresh"
    assert [call[0] for call in client.calls] == ["mock://refresh?refreshToken=refresh-1&appId=app", "mock://token?appId=app&appSecret=secret"]


def test_valid_refresh_token_refreshes_directly_and_updates_cache():
    cache = LingxingTokenCache()
    cache.access_token = "expired"
    cache.expires_at = NOW
    cache.refresh_token = "refresh-1"
    cache.refresh_expires_at = NOW + timedelta(hours=1)
    client = QueueClient(
        [{"access_token": "refreshed-access", "refresh_token": "refresh-2", "expires_in": 3600}]
    )
    service = service_with(client, cache)

    assert service.access_token() == "refreshed-access"
    assert cache.refresh_token == "refresh-2"
    assert client.calls == [("mock://refresh?refreshToken=refresh-1&appId=app", 10)]


# Feature: batch-traceability, Property 39: 令牌请求重试有界且耗尽无副作用
def test_token_request_retries_at_most_three_times_one_second_apart():
    errors = [LingxingError("temporary", retryable=True) for _ in range(3)]
    client = QueueClient(errors)
    sleeps: list[float] = []
    cache = LingxingTokenCache()
    service = service_with(client, cache)
    service.sleeper = sleeps.append

    with pytest.raises(LingxingError, match="temporary"):
        service.access_token()
    assert client.calls == [("mock://token?appId=app&appSecret=secret", 10)] * 3
    assert sleeps == [1, 1]
    assert cache.access_token == ""


def test_push_reauthenticates_once_when_access_token_is_rejected():
    """A stale-token business error clears the cache, re-auths and retries once."""
    cache = LingxingTokenCache()
    cache.access_token = "stale"
    cache.expires_at = NOW + timedelta(hours=1)
    client = QueueClient(
        [
            {"code": 2001008, "message": "access_token invalid"},
            {"access_token": "fresh", "expires_in": 3600},
            {"code": 0, "data": {"poId": "LX-PO-9"}},
        ]
    )
    service = LingxingIntegrationService(
        http_client=client,
        token_cache=cache,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        credential_config={"appId": "1234567890abcdef", "appSecret": "secret"},
        endpoints={
            "token": "mock://token",
            "refresh_token": "mock://refresh",
            "purchase_order": "mock://purchase-order",
        },
    )

    result = service.push("purchase_order", {"poNo": "PO-1"})
    assert result == {"code": 0, "data": {"poId": "LX-PO-9"}}
    assert cache.access_token == "fresh"
    # First business call (stale token) -> token fetch -> retried business call.
    assert [call[0].split("?")[0] for call in client.calls] == [
        "mock://purchase-order",
        "mock://token",
        "mock://purchase-order",
    ]
