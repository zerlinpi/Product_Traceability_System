"""Outbound endpoint policy (SSRF defence for the Lingxing integration).

Before this policy existed, an administrator could point a "push to Lingxing"
button at ``http://169.254.169.254/latest/meta-data/``, at the system's own API
on ``127.0.0.1:5080``, or at any host on the factory LAN, and the server would
fetch it. These tests pin the fix, including the two parts a naive
"validate the URL before the request" check misses: DNS resolution and
redirects.

Feature: batch-traceability, Property 72: 出站地址策略
"""

from __future__ import annotations

import socket
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capability_helpers import make_auth_app  # noqa: E402
from traceability.endpoint_policy import (  # noqa: E402
    ENV_ALLOWED_HOSTS,
    ENV_ALLOW_HTTP,
    ENV_ALLOW_PRIVATE,
    EndpointPolicy,
    EndpointPolicyError,
    PolicyRedirectHandler,
    assert_url_allowed,
    is_internal_address,
    resolve_endpoint,
)
from traceability.lingxing import (  # noqa: E402
    DEFAULT_API_BASE_URL,
    LingxingError,
    LingxingIntegrationService,
    UrllibLingxingHttpClient,
)

STRICT = EndpointPolicy()


# --------------------------------------------------------------------------
# 1. The default policy
# --------------------------------------------------------------------------


def test_default_allows_only_the_official_host():
    assert STRICT.allowed_hosts == frozenset({"openapi.lingxing.com"})
    assert assert_url_allowed(DEFAULT_API_BASE_URL, STRICT, resolve=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/erp/sc/x",
        "https://openapi.lingxing.com.evil.example.com/x",
        "https://notopenapi.lingxing.com/x",
    ],
)
def test_unknown_hosts_are_refused(url):
    with pytest.raises(EndpointPolicyError) as error:
        assert_url_allowed(url, STRICT, resolve=False)
    assert "不在允许列表" in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://openapi.lingxing.com/erp/sc/x",
        "http://127.0.0.1/x",
    ],
)
def test_plain_http_is_refused_by_default(url):
    with pytest.raises(EndpointPolicyError) as error:
        assert_url_allowed(url, STRICT, resolve=False)
    assert "https" in str(error.value)


def test_http_can_be_enabled_for_a_test_environment():
    policy = EndpointPolicy(allow_http=True)
    assert assert_url_allowed("http://openapi.lingxing.com/x", policy, resolve=False)


def test_production_never_allows_http_even_with_the_flag():
    policy = EndpointPolicy.from_environment(
        {"PTS_ENV": "production", ENV_ALLOW_HTTP: "1"}
    )
    assert policy.allow_http is False
    with pytest.raises(EndpointPolicyError):
        assert_url_allowed("http://openapi.lingxing.com/x", policy, resolve=False)


def test_development_can_opt_into_http():
    policy = EndpointPolicy.from_environment({"PTS_ENV": "development", ENV_ALLOW_HTTP: "1"})
    assert policy.allow_http is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://localhost/etc/passwd",
        "ftp://openapi.lingxing.com/x",
        "ftps://openapi.lingxing.com/x",
        "gopher://openapi.lingxing.com/x",
        "data:text/plain,hello",
        "jar:file:///x!/y",
    ],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(EndpointPolicyError):
        assert_url_allowed(url, STRICT, resolve=False)


# --------------------------------------------------------------------------
# 2. Internal targets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.1.2.3", "0.0.0.0", "10.0.0.5", "172.16.0.1", "172.31.255.254",
     "192.168.1.10", "169.254.169.254", "224.0.0.1", "240.0.0.1"],
)
def test_private_and_reserved_ipv4_literals_are_internal(host):
    assert is_internal_address(host) is True


@pytest.mark.parametrize("host", ["::1", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1"])
def test_private_and_reserved_ipv6_literals_are_internal(host):
    assert is_internal_address(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_not_internal(host):
    assert is_internal_address(host) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/x",
        "https://LOCALHOST/x",
        "https://api.localhost/x",
        "https://thing.internal/x",
        "https://printer.local/x",
        "https://metadata.google.internal/x",
        "https://metadata/x",
    ],
)
def test_local_and_internal_names_are_refused(url):
    with pytest.raises(EndpointPolicyError):
        assert_url_allowed(url, STRICT, resolve=False)


def test_an_allowlisted_name_pointing_inward_is_refused(monkeypatch):
    """DNS rebinding: the name is fine, the address it resolves to is not."""
    monkeypatch.setattr(
        "traceability.endpoint_policy.socket.getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(EndpointPolicyError) as error:
        assert_url_allowed("https://openapi.lingxing.com/x", STRICT)
    assert "内网地址" in str(error.value)


def test_resolution_failure_is_not_fatal(monkeypatch):
    """An offline machine must not be unable to configure Lingxing."""
    def boom(*args, **kwargs):
        raise socket.gaierror("no DNS here")

    monkeypatch.setattr("traceability.endpoint_policy.socket.getaddrinfo", boom)
    assert assert_url_allowed("https://openapi.lingxing.com/x", STRICT)


def test_an_ip_literal_that_is_internal_is_refused_even_if_allowlisted():
    policy = EndpointPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
    with pytest.raises(EndpointPolicyError):
        assert_url_allowed("https://127.0.0.1/x", policy, resolve=False)


def test_lab_mode_can_permit_internal_hosts():
    policy = EndpointPolicy(
        allowed_hosts=frozenset({"192.168.1.10"}), allow_private_networks=True
    )
    assert assert_url_allowed("https://192.168.1.10/x", policy, resolve=False)


# --------------------------------------------------------------------------
# 3. URL shape
# --------------------------------------------------------------------------


def test_userinfo_is_refused():
    """``https://openapi.lingxing.com@evil.example.com/`` reads as the official
    host to a human and as ``evil.example.com`` to a parser."""
    with pytest.raises(EndpointPolicyError) as error:
        assert_url_allowed(
            "https://openapi.lingxing.com@evil.example.com/x", STRICT, resolve=False
        )
    assert "用户名" in str(error.value)


def test_an_empty_host_is_refused():
    with pytest.raises(EndpointPolicyError):
        assert_url_allowed("https:///x", STRICT, resolve=False)


def test_an_empty_url_is_refused():
    with pytest.raises(EndpointPolicyError):
        assert_url_allowed("", STRICT, resolve=False)


def test_hostnames_are_compared_case_insensitively():
    assert assert_url_allowed("https://OpenAPI.Lingxing.COM/x", STRICT, resolve=False)


def test_a_trailing_dot_is_normalised():
    assert assert_url_allowed("https://openapi.lingxing.com./x", STRICT, resolve=False)


def test_extra_hosts_can_be_allowlisted():
    policy = EndpointPolicy.from_environment(
        {ENV_ALLOWED_HOSTS: "sandbox.example.com, other.example.com"}
    )
    assert "sandbox.example.com" in policy.allowed_hosts
    assert "other.example.com" in policy.allowed_hosts
    assert assert_url_allowed("https://sandbox.example.com/x", policy, resolve=False)


def test_with_extra_hosts_keeps_the_defaults():
    policy = STRICT.with_extra_hosts(["sandbox.example.com"])
    assert "openapi.lingxing.com" in policy.allowed_hosts
    assert "sandbox.example.com" in policy.allowed_hosts


def test_private_hosts_flag_is_read_from_the_environment():
    assert EndpointPolicy.from_environment({ENV_ALLOW_PRIVATE: "1"}).allow_private_networks
    assert not EndpointPolicy.from_environment({}).allow_private_networks


# --------------------------------------------------------------------------
# 4. Relative paths
# --------------------------------------------------------------------------


def test_a_relative_path_is_joined_onto_the_base():
    resolved = resolve_endpoint(
        "/erp/sc/routing/purchase/purchase/setOrders",
        base_url=DEFAULT_API_BASE_URL,
        policy=STRICT,
    )
    assert resolved == f"{DEFAULT_API_BASE_URL}/erp/sc/routing/purchase/purchase/setOrders"


def test_a_relative_path_without_a_leading_slash_still_works():
    resolved = resolve_endpoint("erp/sc/x", base_url=DEFAULT_API_BASE_URL, policy=STRICT)
    assert resolved == f"{DEFAULT_API_BASE_URL}/erp/sc/x"


def test_a_relative_path_cannot_escape_the_base_host():
    """The whole point of preferring relative paths."""
    resolved = resolve_endpoint("/x", base_url=DEFAULT_API_BASE_URL, policy=STRICT)
    assert resolved.startswith(DEFAULT_API_BASE_URL)


def test_an_empty_endpoint_resolves_to_empty():
    assert resolve_endpoint("", base_url=DEFAULT_API_BASE_URL, policy=STRICT) == ""


def test_a_bad_base_url_is_refused():
    with pytest.raises(EndpointPolicyError):
        resolve_endpoint("/x", base_url="http://169.254.169.254", policy=STRICT)


def test_an_absolute_url_that_fails_policy_is_refused():
    with pytest.raises(EndpointPolicyError):
        resolve_endpoint("https://evil.example.com/x", base_url=DEFAULT_API_BASE_URL, policy=STRICT)


# --------------------------------------------------------------------------
# 5. Redirects
# --------------------------------------------------------------------------


def _redirect(handler: PolicyRedirectHandler, newurl: str):
    request = urllib.request.Request("https://openapi.lingxing.com/x")
    return handler.redirect_request(request, None, 302, "Found", {}, newurl)


def test_a_redirect_to_an_unlisted_host_is_refused():
    """A permitted host answering ``302`` to an internal address must not be
    followed — validating only the original URL would miss this entirely."""
    handler = PolicyRedirectHandler(STRICT)
    with pytest.raises(EndpointPolicyError):
        _redirect(handler, "http://169.254.169.254/latest/meta-data/")


def test_a_redirect_to_an_internal_name_is_refused():
    handler = PolicyRedirectHandler(STRICT)
    with pytest.raises(EndpointPolicyError):
        _redirect(handler, "https://localhost/admin")


def test_a_redirect_within_the_allowlist_is_followed():
    handler = PolicyRedirectHandler(STRICT)
    followed = _redirect(handler, "https://openapi.lingxing.com/other")
    assert followed is not None
    assert followed.full_url == "https://openapi.lingxing.com/other"


# --------------------------------------------------------------------------
# 6. Transport layer
# --------------------------------------------------------------------------


def test_the_transport_refuses_a_forbidden_url():
    client = UrllibLingxingHttpClient(STRICT)
    with pytest.raises(LingxingError) as error:
        client.request("POST", "http://169.254.169.254/latest/meta-data/", json_data={}, headers={}, timeout=5)
    assert error.value.status == 503
    assert error.value.retryable is False, "a policy rejection must never be retried"


def test_the_transport_refuses_a_forbidden_url_before_opening_a_socket():
    """No DNS, no connection: the check happens first."""
    client = UrllibLingxingHttpClient(STRICT)
    with pytest.raises(LingxingError):
        client.request("POST", "https://evil.example.com/x", json_data={}, headers={}, timeout=5)


def test_the_transport_refuses_an_empty_url():
    client = UrllibLingxingHttpClient(STRICT)
    with pytest.raises(LingxingError):
        client.request("POST", "", json_data={}, headers={}, timeout=5)


def test_the_transport_uses_a_policy_aware_opener():
    client = UrllibLingxingHttpClient(STRICT)
    handlers = [handler for handler in client._opener.handlers if isinstance(handler, PolicyRedirectHandler)]
    assert handlers, "the opener must enforce the policy on redirects"


# --------------------------------------------------------------------------
# 7. Service construction
# --------------------------------------------------------------------------


def test_the_service_refuses_a_forbidden_base_url():
    with pytest.raises(LingxingError) as error:
        LingxingIntegrationService(api_base_url="http://169.254.169.254")
    assert error.value.retryable is False


def test_the_service_accepts_the_official_base_url():
    service = LingxingIntegrationService()
    assert service.api_base_url == DEFAULT_API_BASE_URL


def test_the_service_passes_its_policy_to_the_default_transport():
    # The policy must still allow the base URL, or construction fails first.
    policy = STRICT.with_extra_hosts(["sandbox.example.com"])
    service = LingxingIntegrationService(policy=policy)
    assert service.http_client.policy is policy


def test_a_policy_that_excludes_the_base_url_fails_at_construction():
    """Better to refuse to start than to build a service that cannot work."""
    policy = EndpointPolicy(allowed_hosts=frozenset({"sandbox.example.com"}))
    with pytest.raises(LingxingError):
        LingxingIntegrationService(policy=policy)


def test_an_injected_transport_is_left_alone():
    """Tests and deployments inject their own transport; the policy must not
    replace it."""
    sentinel = object()
    service = LingxingIntegrationService(http_client=sentinel)
    assert service.http_client is sentinel


# --------------------------------------------------------------------------
# 8. Settings validation
# --------------------------------------------------------------------------


@pytest.fixture()
def app_context(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    with app.app_context():
        yield app


@pytest.mark.parametrize(
    "value",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:5080/api/users",
        "https://evil.example.com/x",
        "file:///etc/passwd",
        "http://openapi.lingxing.com/x",
        "https://localhost/x",
    ],
)
def test_saving_a_forbidden_endpoint_is_rejected(app_context, value):
    from app import clean_lingxing_endpoint

    with pytest.raises(Exception) as error:
        clean_lingxing_endpoint(value, "采购订单写入接口")
    assert "接口" in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/erp/sc/routing/purchase/purchase/setOrders",
        "https://openapi.lingxing.com/erp/sc/x",
    ],
)
def test_saving_an_acceptable_endpoint_is_allowed(app_context, value):
    from app import clean_lingxing_endpoint

    assert clean_lingxing_endpoint(value, "采购订单写入接口") == value


def test_a_path_without_a_leading_slash_is_still_refused(app_context):
    """Pre-existing rule: the stored form is a ``/path`` or a URL.

    ``_endpoint_url`` would tolerate a bare ``erp/sc/x`` at request time, but the
    save-time contract stays strict so what is stored is unambiguous.
    """
    from app import clean_lingxing_endpoint

    with pytest.raises(Exception):
        clean_lingxing_endpoint("erp/sc/x", "采购订单写入接口")


def test_saving_a_relative_path_is_the_recommended_form(app_context):
    from app import clean_lingxing_endpoint

    saved = clean_lingxing_endpoint("/erp/sc/x", "入库写入接口")
    assert saved.startswith("/"), "a relative path cannot change the host"
