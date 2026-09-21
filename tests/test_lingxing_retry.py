from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from traceability.lingxing import LingxingError, LingxingIntegrationService, RetryConfig


class RetryClient:
    def __init__(self, failures: int, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.calls: list[int] = []

    def request(self, method, url, *, json_data, headers, timeout):
        self.calls.append(timeout)
        if len(self.calls) <= self.failures:
            raise LingxingError("mock failure", retryable=self.retryable)
        return {"ok": True}


# Feature: batch-traceability, Property 52: 重试配置的边界与默认值
@settings(max_examples=100, deadline=None)
@given(
    max_retries=st.integers(min_value=0, max_value=10),
    timeout=st.integers(min_value=1, max_value=120),
    interval=st.integers(min_value=1, max_value=60),
)
def test_retry_configuration_accepts_exact_documented_ranges(max_retries, timeout, interval):
    config = RetryConfig(max_retries, timeout, interval)
    assert (config.max_retries, config.request_timeout, config.retry_interval) == (
        max_retries,
        timeout,
        interval,
    )
    assert RetryConfig() == RetryConfig(3, 30, 2)


@pytest.mark.parametrize(
    "values",
    [(-1, 30, 2), (11, 30, 2), (3, 0, 2), (3, 121, 2), (3, 30, 0), (3, 30, 61)],
)
def test_retry_configuration_rejects_out_of_range_values(values):
    with pytest.raises(ValueError):
        RetryConfig(*values)


# Feature: batch-traceability, Property 53: 可重试错误的有界重试执行
@settings(max_examples=100, deadline=None)
@given(
    max_retries=st.integers(min_value=0, max_value=5),
    failures=st.integers(min_value=0, max_value=8),
    timeout=st.integers(min_value=1, max_value=120),
    interval=st.integers(min_value=1, max_value=10),
)
def test_retry_executor_is_bounded_and_uses_configured_timeout(
    max_retries, failures, timeout, interval
):
    client = RetryClient(failures)
    sleeps: list[float] = []
    service = LingxingIntegrationService(
        http_client=client,
        sleeper=sleeps.append,
        retry_config=RetryConfig(max_retries, timeout, interval),
    )

    if failures > max_retries:
        with pytest.raises(LingxingError, match="mock failure"):
            service._request_with_retry("POST", "mock://api", {"id": 1}, {})
    else:
        assert service._request_with_retry("POST", "mock://api", {"id": 1}, {}) == {"ok": True}

    assert len(client.calls) == min(failures + 1, max_retries + 1)
    assert client.calls == [timeout] * len(client.calls)
    assert sleeps == [interval] * (len(client.calls) - 1)


def test_non_retryable_error_is_not_retried():
    client = RetryClient(failures=10, retryable=False)
    sleeps: list[float] = []
    service = LingxingIntegrationService(http_client=client, sleeper=sleeps.append)
    with pytest.raises(LingxingError):
        service._request_with_retry("POST", "mock://api", {"id": 1}, {})
    assert len(client.calls) == 1
    assert sleeps == []

