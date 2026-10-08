"""A timeout is an unknown outcome, not a failure.

Why this file exists
--------------------
The retry loop treated every network error the same: mark it retryable, send it
again. For a write that is the wrong question. A connection refused means nothing
was sent, so retrying is free. A timeout means the request *was* sent and the
reply never came — Lingxing may have created the purchase order already. Sending
it again is how one order becomes two, and the operator is then told the first
attempt "failed", which is not what happened.

The rules these tests hold:

* a timeout is ``uncertain`` and is not retried on a write;
* a connection failure is still retried, because nothing left the process;
* minting a token is idempotent, so that path may retry a timeout;
* the push route records the unknown outcome distinctly and tells the operator to
  check Lingxing before trying again.
"""

from __future__ import annotations

import socket
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from traceability.lingxing import (  # noqa: E402
    LingxingError,
    LingxingIntegrationService,
    RetryConfig,
    UrllibLingxingHttpClient,
)


class RaisingClient:
    """A transport that always raises the error it was given."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def request(self, method, url, *, json_data, headers, timeout):
        self.calls += 1
        raise self.error


def service_with(client, *, max_retries: int = 3) -> LingxingIntegrationService:
    return LingxingIntegrationService(
        http_client=client,
        sleeper=lambda _seconds: None,
        retry_config=RetryConfig(max_retries, 30, 2),
    )


# --------------------------------------------------------------------------
# The retry policy
# --------------------------------------------------------------------------


def test_a_timeout_on_a_write_is_not_retried():
    """The core rule. One attempt, because the first one may have landed."""
    client = RaisingClient(LingxingError("超时", uncertain=True))
    service = service_with(client)

    with pytest.raises(LingxingError) as error:
        service._request_with_retry("POST", "mock://api", {"id": 1}, {})

    assert client.calls == 1, "写操作超时后重试了"
    assert error.value.uncertain is True


def test_a_timeout_while_minting_a_token_is_retried():
    """Minting a token has no side effect worth duplicating."""
    client = RaisingClient(LingxingError("超时", uncertain=True))
    service = service_with(client, max_retries=2)

    with pytest.raises(LingxingError):
        service._request_with_retry("POST", "mock://api", None, {}, token_request=True)

    assert client.calls == 3, "token 请求应重试到上限"


def test_a_connection_failure_is_still_retried_on_a_write():
    """Nothing was sent, so sending it again cannot duplicate anything."""
    client = RaisingClient(LingxingError("连接被拒绝", retryable=True))
    service = service_with(client, max_retries=2)

    with pytest.raises(LingxingError):
        service._request_with_retry("POST", "mock://api", {"id": 1}, {})

    assert client.calls == 3


def test_an_ordinary_retryable_error_is_unaffected():
    client = RaisingClient(LingxingError("HTTP 503", retryable=True))
    service = service_with(client, max_retries=1)

    with pytest.raises(LingxingError):
        service._request_with_retry("POST", "mock://api", {"id": 1}, {})

    assert client.calls == 2


def test_a_business_rejection_is_never_retried():
    client = RaisingClient(LingxingError("参数错误", retryable=False))
    service = service_with(client)

    with pytest.raises(LingxingError):
        service._request_with_retry("POST", "mock://api", {"id": 1}, {})

    assert client.calls == 1


def test_an_error_is_not_uncertain_unless_it_says_so():
    """The flag must be opt-in, or every failure would look like an unknown."""
    assert LingxingError("普通失败").uncertain is False
    assert LingxingError("普通失败", retryable=True).uncertain is False
    assert LingxingError("超时", uncertain=True).uncertain is True


def test_an_uncertain_error_is_not_also_retryable():
    """The two flags answer different questions and must not be conflated."""
    error = LingxingError("超时", uncertain=True)

    assert error.retryable is False, "结果未知的错误不应被标为可重试"


# --------------------------------------------------------------------------
# The transport decides which of the two a failure is
# --------------------------------------------------------------------------


class FakeOpener:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


def client_raising(error: BaseException) -> UrllibLingxingHttpClient:
    client = UrllibLingxingHttpClient()
    client._opener = FakeOpener(error)
    return client


def test_a_socket_timeout_becomes_uncertain():
    client = client_raising(TimeoutError("timed out"))

    with pytest.raises(LingxingError) as error:
        client.request("POST", "https://openapi.lingxing.com/x", json_data={}, headers={}, timeout=5)

    assert error.value.uncertain is True
    assert error.value.retryable is False
    assert "结果未知" in error.value.message or "未知" in error.value.message


def test_a_url_error_wrapping_a_timeout_is_also_uncertain():
    """urllib wraps the socket error, so the reason has to be inspected."""
    wrapped = urllib.error.URLError(socket.timeout("timed out"))
    client = client_raising(wrapped)

    with pytest.raises(LingxingError) as error:
        client.request("POST", "https://openapi.lingxing.com/x", json_data={}, headers={}, timeout=5)

    assert error.value.uncertain is True


def test_a_connection_refusal_is_retryable_and_not_uncertain():
    """Nothing was sent, so this is a plain failure with a safe retry."""
    refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
    client = client_raising(refused)

    with pytest.raises(LingxingError) as error:
        client.request("POST", "https://openapi.lingxing.com/x", json_data={}, headers={}, timeout=5)

    assert error.value.retryable is True
    assert error.value.uncertain is False


def test_a_dns_failure_is_retryable_and_not_uncertain():
    client = client_raising(urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))

    with pytest.raises(LingxingError) as error:
        client.request("POST", "https://openapi.lingxing.com/x", json_data={}, headers={}, timeout=5)

    assert error.value.retryable is True
    assert error.value.uncertain is False
