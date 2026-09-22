"""Outbound endpoint policy for the Lingxing integration (SSRF defence).

The problem
-----------
The Lingxing write endpoints are administrator-editable, and the previous
validation accepted **any** ``http(s)://`` URL. An administrator (or anyone who
obtains an administrator session) could therefore point a "push to Lingxing"
button at ``http://169.254.169.254/latest/meta-data/`` (cloud metadata),
``http://127.0.0.1:5080/api/users`` (the system's own API), or a host on the
factory LAN — and the server would fetch it and surface the response. That is a
server-side request forgery primitive, not a configuration mistake.

The policy
----------
1. The only host allowed by default is the official ``openapi.lingxing.com``.
2. A custom endpoint may be a **relative path** — it is joined onto the
   configured base URL, so it cannot change the host at all.
3. A custom **host** requires an explicit allowlist
   (``PTS_LINGXING_ALLOWED_HOSTS``). Nothing is allowlisted implicitly.
4. HTTP is refused unless explicitly enabled, and never in production.
5. Loopback, link-local, private, reserved and multicast addresses are refused
   even when the host is allowlisted, and hostnames are resolved so an
   allowlisted *name* cannot point at an internal address.
6. Redirects are re-validated. A URL that passes the checks and then answers
   ``302 Location: http://169.254.169.254/`` must not be followed — this is the
   part a naive "validate the URL before the request" check misses.

Deliberately Flask-free, like ``traceability.capabilities``, so it can be
tested and reasoned about directly.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Mapping

DEFAULT_ALLOWED_HOSTS = frozenset({"openapi.lingxing.com"})

# Schemes that must never be fetched. ``urlopen`` would happily read ``file://``.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Names that resolve inward on typical deployments. Blocked by name so the
# check does not depend on DNS being available.
FORBIDDEN_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
FORBIDDEN_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "metadata"})

ENV_ALLOWED_HOSTS = "PTS_LINGXING_ALLOWED_HOSTS"
ENV_ALLOW_HTTP = "PTS_LINGXING_ALLOW_HTTP"
ENV_ALLOW_PRIVATE = "PTS_LINGXING_ALLOW_PRIVATE_HOSTS"


class EndpointPolicyError(ValueError):
    """The endpoint is not permitted by policy."""


def _flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EndpointPolicy:
    """Who this server is allowed to talk to on behalf of the Lingxing feature."""

    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    allow_http: bool = False
    allow_private_networks: bool = False
    production: bool = False

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "EndpointPolicy":
        env = os.environ if environment is None else environment
        extra = {
            host.strip().lower().rstrip(".")
            for host in str(env.get(ENV_ALLOWED_HOSTS) or "").split(",")
            if host.strip()
        }
        production = str(env.get("PTS_ENV") or "development").strip().lower() == "production"
        # Production never permits plaintext HTTP, whatever the flag says.
        allow_http = _flag(env.get(ENV_ALLOW_HTTP)) and not production
        return cls(
            allowed_hosts=frozenset(DEFAULT_ALLOWED_HOSTS | extra),
            allow_http=allow_http,
            allow_private_networks=_flag(env.get(ENV_ALLOW_PRIVATE)),
            production=production,
        )

    def with_extra_hosts(self, hosts: object) -> "EndpointPolicy":
        extra = {
            str(host).strip().lower().rstrip(".")
            for host in (hosts or [])
            if str(host).strip()
        }
        return replace(self, allowed_hosts=self.allowed_hosts | frozenset(extra))

    @property
    def default_base_url(self) -> str:
        return "https://openapi.lingxing.com"


def is_internal_address(value: str) -> bool:
    """True for loopback / link-local / private / reserved / multicast addresses."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_multicast,
            address.is_unspecified,
        )
    )


def _resolve_addresses(hostname: str) -> list[str]:
    """Best-effort DNS resolution. Failure is not fatal: the request will fail."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (OSError, UnicodeError):
        return []
    addresses = []
    for info in infos:
        candidate = info[4][0]
        if candidate not in addresses:
            addresses.append(candidate)
    return addresses


def assert_url_allowed(
    url: str,
    policy: EndpointPolicy,
    *,
    label: str = "接口地址",
    resolve: bool = True,
) -> str:
    """Validate an absolute URL against ``policy``; return it unchanged if allowed."""
    text = str(url or "").strip()
    if not text:
        raise EndpointPolicyError(f"{label}未配置")

    parts = urllib.parse.urlsplit(text)
    scheme = (parts.scheme or "").lower()

    if not scheme:
        raise EndpointPolicyError(f"{label}必须是完整链接或相对路径")
    if scheme not in ALLOWED_SCHEMES:
        raise EndpointPolicyError(
            f"{label}不支持 {scheme}:// 协议，仅允许 http(s)"
        )
    if scheme == "http" and not policy.allow_http:
        raise EndpointPolicyError(
            f"{label}必须使用 https（如需在测试环境使用 http，请设置 {ENV_ALLOW_HTTP}=1）"
        )
    if parts.username or parts.password:
        raise EndpointPolicyError(f"{label}不能包含用户名或密码")

    hostname = (parts.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise EndpointPolicyError(f"{label}缺少主机名")

    if hostname in FORBIDDEN_HOSTNAMES or hostname.endswith(FORBIDDEN_HOST_SUFFIXES):
        raise EndpointPolicyError(f"{label}指向本机或内部名称（{hostname}），已拒绝")

    if hostname not in policy.allowed_hosts:
        allowed = "、".join(sorted(policy.allowed_hosts))
        raise EndpointPolicyError(
            f"{label}的主机 {hostname} 不在允许列表中。"
            f"允许：{allowed}。如需使用其他主机，请设置 {ENV_ALLOWED_HOSTS}"
        )

    if not policy.allow_private_networks:
        # An allowlisted *name* must not point at an internal address, and an
        # IP literal must not be internal either.
        if is_internal_address(hostname):
            raise EndpointPolicyError(f"{label}指向内网或本机地址（{hostname}），已拒绝")
        if resolve:
            for address in _resolve_addresses(hostname):
                if is_internal_address(address):
                    raise EndpointPolicyError(
                        f"{label}的域名 {hostname} 解析到内网地址 {address}，已拒绝"
                    )
    return text


def resolve_endpoint(
    value: object,
    *,
    base_url: str,
    policy: EndpointPolicy,
    label: str = "接口地址",
) -> str:
    """Turn a stored endpoint value into an absolute, policy-checked URL.

    A value without a scheme is treated as a path on ``base_url``, so the
    common case (an administrator pastes ``/erp/sc/routing/...``) cannot change
    which host is contacted at all.
    """
    text = str(value or "").strip()
    if not text:
        return ""

    base = str(base_url or policy.default_base_url).strip().rstrip("/")
    assert_url_allowed(base, policy, label=f"{label}基础地址")

    if "://" in text:
        return assert_url_allowed(text, policy, label=label)

    if not text.startswith("/"):
        text = "/" + text
    return assert_url_allowed(f"{base}{text}", policy, label=label)


class PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect that leaves the allowlist.

    Without this, validating the URL before the request is not enough: the
    permitted host can answer ``302 Location: http://169.254.169.254/`` and
    ``urlopen`` would follow it.
    """

    def __init__(self, policy: EndpointPolicy) -> None:
        super().__init__()
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        assert_url_allowed(newurl, self.policy, label="重定向目标")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(policy: EndpointPolicy) -> urllib.request.OpenerDirector:
    """An opener whose redirect handling enforces ``policy``."""
    return urllib.request.build_opener(PolicyRedirectHandler(policy))
