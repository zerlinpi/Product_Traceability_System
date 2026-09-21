from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from Crypto.Cipher import AES

from traceability.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyError,
    assert_url_allowed,
    build_opener,
)


DEFAULT_API_BASE_URL = "https://openapi.lingxing.com"
DEFAULT_TOKEN_PATH = "/api/auth-server/oauth/access-token"
DEFAULT_REFRESH_TOKEN_PATH = "/api/auth-server/oauth/refresh"
# 采购单下单: moves an existing Lingxing purchase order from 待下单 to 待到货.
# It takes ``{"order_sn": ["PO210705007"]}`` and answers with an empty ``data``
# list on success, so it neither creates an order nor returns an object id.
DEFAULT_PURCHASE_ORDER_PATH = "/erp/sc/routing/purchase/purchase/setOrders"
# 查询收货单列表: lists purchase 收货单 (order_type=1) so a 收货单号 (order_sn, e.g.
# ``CR240729025``) and its 收货单子项id (``item_list[].item_id``) can be resolved
# from the source 采购单号 (``business_order_sn``). Answers with
# ``data.list[]`` (and ``data.total``).
DEFAULT_RECEIPT_LIST_PATH = "/erp/sc/routing/deliveryReceipt/PurchaseReceiptOrder/getOrderList"
# 快捷入库 (fastReceive): receives a 收货单 in one shot. It takes an ``order_sn``
# plus ``item_list`` of ``{id, product_good_num, product_bad_num}`` and answers
# with an empty ``data`` list on success (no object id), like 采购单下单.
DEFAULT_INVENTORY_RECEIVE_PATH = (
    "/erp/sc/routing/deliveryReceipt/PurchaseReceiptOrder/fastReceive"
)


class LingxingError(Exception):
    """Stable application-facing error raised by the Lingxing adapter."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int = 502,
        code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status
        # The upstream Lingxing business code (e.g. "2001008") when the error
        # originates from a decoded API envelope; used to detect expired tokens.
        self.code = code


# Lingxing business codes that mean the access token is stale/invalid. When the
# API answers with one of these, re-authenticating and retrying once resolves it
# (a plain retry with the same token would keep failing).
TOKEN_INVALID_CODES = {"2001008", "2001009", "3001008"}


def mask_secret(value: object) -> str:
    """Mask a credential/token and expose at most its final four characters."""

    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


@dataclass(frozen=True)
class LingxingCredentials:
    app_id: str = ""
    app_secret: str = ""
    # Retained for backward compatibility with v14 databases. The current
    # Lingxing OpenAPI authenticates with AppID and AppSecret only.
    account_id: str = ""

    @property
    def configured(self) -> bool:
        return all(value.strip() for value in (self.app_id, self.app_secret))

    def masked(self) -> dict[str, str]:
        return {
            "appId": mask_secret(self.app_id),
            "appSecret": mask_secret(self.app_secret),
        }


SETTING_KEYS = {
    "app_id": "lingxing.app_id",
    "app_secret": "lingxing.app_secret",
    "account_id": "lingxing.id",
}


def load_credentials(
    database=None,
    *,
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> LingxingCredentials:
    """Load credentials from DB first, then environment, then optional config."""

    stored: dict[str, str] = {}
    if database is not None:
        try:
            rows = database.execute(
                "SELECT setting_key, setting_value FROM app_settings WHERE setting_key IN (?, ?, ?)",
                tuple(SETTING_KEYS.values()),
            ).fetchall()
            stored = {str(row["setting_key"]): str(row["setting_value"] or "") for row in rows}
        except Exception:
            # Databases below v14 do not have app_settings. Local operation must
            # remain available, so credential lookup safely falls through.
            stored = {}

    env = environ if environ is not None else os.environ
    cfg = config or {}

    def pick(setting_key: str, env_key: str, config_key: str) -> str:
        stored_value = stored.get(setting_key, "").strip()
        if stored_value:
            return stored_value
        env_value = str(env.get(env_key, "") or "").strip()
        if env_value:
            return env_value
        return str(cfg.get(config_key, "") or "").strip()

    return LingxingCredentials(
        app_id=pick(SETTING_KEYS["app_id"], "PTS_LINGXING_APP_ID", "appId"),
        app_secret=pick(SETTING_KEYS["app_secret"], "PTS_LINGXING_APP_SECRET", "appSecret"),
        account_id=pick(SETTING_KEYS["account_id"], "PTS_LINGXING_ID", "id"),
    )


class SignStrategy(Protocol):
    def sign(self, params: Mapping[str, Any], access_token: str, timestamp: str) -> str: ...


class DefaultSignStrategy:
    """Lingxing OpenAPI MD5 + AES-ECB signature implementation."""

    def __init__(self, app_id: str) -> None:
        self.app_id = str(app_id or "").strip()

    def sign(self, params: Mapping[str, Any], access_token: str, timestamp: str) -> str:
        if not access_token or not timestamp:
            raise LingxingError("无法生成请求签名", status=400)
        key = self.app_id.encode("utf-8")
        if len(key) not in {16, 24, 32}:
            raise LingxingError("领星 AppID 必须是 16、24 或 32 字节", status=400)

        sign_params = {
            "app_key": self.app_id,
            "access_token": access_token,
            "timestamp": timestamp,
            **dict(params),
        }
        items: list[str] = []
        for name in sorted(sign_params):
            value = sign_params[name]
            if value is None or value == "":
                continue
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            items.append(f"{name}={value}")
        canonical = "&".join(items)
        md5_hex = hashlib.md5(canonical.encode("utf-8")).hexdigest().upper()
        padding_length = AES.block_size - len(md5_hex) % AES.block_size
        padded = md5_hex.encode("utf-8") + bytes([padding_length]) * padding_length
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(padded)
        # Return the raw base64 signature. The transport layer URL-encodes the
        # query exactly once; pre-encoding here would double-encode the sign
        # (e.g. "+" -> "%2B" -> "%252B") and break signature verification.
        return base64.b64encode(encrypted).decode("ascii")


def compute_sign(
    params: Mapping[str, Any],
    access_token: str,
    timestamp: str,
    strategy: SignStrategy | None = None,
    *,
    app_id: str = "",
) -> str:
    if not params:
        raise LingxingError("无法生成请求签名", status=400)
    return (strategy or DefaultSignStrategy(app_id)).sign(params, access_token, timestamp)


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 3
    request_timeout: int = 30
    retry_interval: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= 10:
            raise ValueError("最大重试次数需在 0-10 之间")
        if isinstance(self.request_timeout, bool) or not 1 <= self.request_timeout <= 120:
            raise ValueError("请求超时需在 1-120 秒之间")
        if isinstance(self.retry_interval, bool) or not 1 <= self.retry_interval <= 60:
            raise ValueError("重试间隔需在 1-60 秒之间")


class LingxingHttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json_data: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        timeout: int,
    ) -> Mapping[str, Any]: ...


class UrllibLingxingHttpClient:
    """Minimal urllib-based transport for Lingxing OpenAPI calls.

    Every outbound request is validated against an :class:`EndpointPolicy`, and
    redirects are re-validated. Validating only where the URL is configured is
    not enough: a permitted host can answer ``302`` to an internal address and
    ``urlopen`` would follow it.
    """

    def __init__(self, policy: EndpointPolicy | None = None) -> None:
        self.policy = policy or EndpointPolicy()
        self._opener = build_opener(self.policy)

    def request(
        self,
        method: str,
        url: str,
        *,
        json_data: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        timeout: int,
    ) -> Mapping[str, Any]:
        if not url:
            raise LingxingError("领星接口地址未配置", status=503)
        try:
            assert_url_allowed(url, self.policy, label="领星接口地址")
        except EndpointPolicyError as error:
            # A policy rejection is a configuration problem, not a transient
            # network failure: never retry it.
            raise LingxingError(str(error), status=503, retryable=False) from error
        body = (
            json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            if json_data is not None
            else None
        )
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener.open(req, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            detail = ""
            try:
                raw_error = error.read(4096).decode("utf-8", errors="replace")
                error_payload = json.loads(raw_error)
                if isinstance(error_payload, Mapping):
                    detail = str(error_payload.get("message") or error_payload.get("msg") or "").strip()
            except (OSError, ValueError, TypeError):
                detail = ""
            suffix = f"：{detail}" if detail else ""
            raise LingxingError(
                f"领星接口返回 HTTP {error.code}{suffix}",
                retryable=retryable,
            ) from error
        except EndpointPolicyError as error:
            raise LingxingError(str(error), status=503, retryable=False) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise LingxingError(f"领星网络请求失败：{error}", retryable=True) from error


class LingxingTokenCache:
    """Thread-safe process-local token cache with a 60-second safety margin."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at: datetime | None = None
        self.refresh_expires_at: datetime | None = None

    def is_access_valid(self, now: datetime) -> bool:
        with self._lock:
            return bool(
                self.access_token
                and self.expires_at
                and now < self.expires_at - timedelta(seconds=60)
            )

    def is_refresh_valid(self, now: datetime) -> bool:
        with self._lock:
            return bool(
                self.refresh_token
                and (self.refresh_expires_at is None or now < self.refresh_expires_at)
            )

    def update(self, payload: Mapping[str, Any], now: datetime) -> None:
        access_token = str(payload.get("access_token") or payload.get("accessToken") or "").strip()
        if not access_token:
            raise LingxingError("领星令牌响应缺少 access_token")
        expires_in = int(payload.get("expires_in") or payload.get("expiresIn") or 3600)
        refresh_token = str(payload.get("refresh_token") or payload.get("refreshToken") or "").strip()
        refresh_expires_in = payload.get("refresh_expires_in") or payload.get("refreshExpiresIn")
        with self._lock:
            self.access_token = access_token
            if refresh_token:
                self.refresh_token = refresh_token
            self.expires_at = now + timedelta(seconds=max(expires_in, 1))
            if refresh_expires_in is not None:
                self.refresh_expires_at = now + timedelta(seconds=max(int(refresh_expires_in), 1))

    def clear(self) -> None:
        with self._lock:
            self.access_token = ""
            self.refresh_token = ""
            self.expires_at = None
            self.refresh_expires_at = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_response(value: Any) -> Any:
    """Recursively mask token/secret values before persistence or logging."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "appsecret",
                "app_secret",
                "accesstoken",
                "access_token",
                "refreshtoken",
                "refresh_token",
            }:
                result[str(key)] = mask_secret(item)
            else:
                result[str(key)] = sanitize_response(item)
        return result
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    return value


class LingxingIntegrationService:
    """Configurable anti-corruption layer used by the Flask route layer."""

    def __init__(
        self,
        *,
        database=None,
        http_client: LingxingHttpClient | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        retry_config: RetryConfig | None = None,
        sign_strategy: SignStrategy | None = None,
        endpoints: Mapping[str, str] | None = None,
        credential_config: Mapping[str, Any] | None = None,
        token_cache: LingxingTokenCache | None = None,
        api_base_url: str = DEFAULT_API_BASE_URL,
        policy: EndpointPolicy | None = None,
    ) -> None:
        self.database = database
        self.policy = policy or EndpointPolicy()
        # The base URL decides which host a relative endpoint lands on, so it is
        # checked here rather than only at request time.
        base = str(api_base_url or DEFAULT_API_BASE_URL).strip().rstrip("/")
        try:
            self.api_base_url = assert_url_allowed(base, self.policy, label="领星基础地址")
        except EndpointPolicyError as error:
            raise LingxingError(str(error), status=503, retryable=False) from error
        self.http_client = http_client or UrllibLingxingHttpClient(self.policy)
        self.clock = clock or _utc_now
        self.sleeper = sleeper or time.sleep
        self.retry_config = retry_config or RetryConfig()
        self.sign_strategy = sign_strategy
        self.endpoints = {
            "token": DEFAULT_TOKEN_PATH,
            "refresh_token": DEFAULT_REFRESH_TOKEN_PATH,
            **dict(endpoints or {}),
        }
        self.credential_config = dict(credential_config or {})
        self.token_cache = token_cache or LingxingTokenCache()

    def credentials(self) -> LingxingCredentials:
        credentials = load_credentials(self.database, config=self.credential_config)
        if not credentials.configured:
            raise LingxingError("领星凭据未配置", status=503)
        return credentials

    def ensure_operation_ready(self, operation: str) -> LingxingCredentials:
        credentials = self.credentials()
        self._endpoint_url(self.endpoints.get(operation, ""))
        return credentials

    def _request_with_retry(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        *,
        token_request: bool = False,
    ) -> Mapping[str, Any]:
        max_retries = 2 if token_request else self.retry_config.max_retries
        timeout = 10 if token_request else self.retry_config.request_timeout
        interval = 1 if token_request else self.retry_config.retry_interval
        last_error: LingxingError | None = None
        for attempt in range(max_retries + 1):
            try:
                response = self.http_client.request(
                    method,
                    self._endpoint_url(url),
                    json_data=payload,
                    headers=headers,
                    timeout=timeout,
                )
                return self._validated_response(response)
            except LingxingError as error:
                last_error = error
                if not error.retryable or attempt >= max_retries:
                    raise
            except (ConnectionError, TimeoutError, OSError) as error:
                last_error = LingxingError(str(error), retryable=True)
                if attempt >= max_retries:
                    raise last_error from error
            self.sleeper(interval)
        raise last_error or LingxingError("领星请求失败")

    def _endpoint_url(
        self,
        endpoint: str,
        query: Mapping[str, Any] | None = None,
    ) -> str:
        endpoint = str(endpoint or "").strip()
        if not endpoint:
            raise LingxingError("领星接口地址未配置", status=503)
        # Deliberately a pure URL builder: the policy is enforced where bytes
        # actually leave the process (``UrllibLingxingHttpClient``) and when an
        # administrator saves the setting. Validating here as well would mean
        # every test that injects a fake transport has to speak real URLs to
        # exercise unrelated logic.
        if not urllib.parse.urlsplit(endpoint).scheme:
            endpoint = f"{self.api_base_url}/{endpoint.lstrip('/')}"
        if not query:
            return endpoint
        encoded = urllib.parse.urlencode(
            {key: value for key, value in query.items() if value is not None and value != ""}
        )
        if not encoded:
            return endpoint
        separator = "&" if urllib.parse.urlsplit(endpoint).query else "?"
        return f"{endpoint}{separator}{encoded}"

    @staticmethod
    def item_failure_details(response: Mapping[str, Any]) -> list[str]:
        """Collect per-item rejection reasons from a Lingxing envelope.

        Batch endpoints such as 采购单下单 (``setOrders``) answer with an empty
        ``data`` list on success and, when an item is rejected, with entries like
        ``{"order_sn": "PO211112002", "detail": "错误：采购单状态已变更"}``. The
        envelope's ``message`` stays ``"success"`` in that case, so these
        per-item details are the only usable explanation. ``error_details`` is
        collected the same way.
        """
        details: list[str] = []
        # Only an explicit failure key marks an entry as rejected; a generic
        # ``message`` is ignored so ordinary success payloads that echo one are
        # never mistaken for errors.
        reason_keys = ("detail", "details", "error", "reason", "errmsg", "err_msg")

        def describe(entry: Any) -> None:
            if not isinstance(entry, Mapping):
                return
            reason = ""
            for key in reason_keys:
                value = entry.get(key)
                if value not in (None, "", [], {}):
                    reason = str(value).strip()
                    break
            if not reason:
                return
            subject = ""
            for key in ("order_sn", "orderSn", "sn", "code", "id"):
                value = entry.get(key)
                if value not in (None, ""):
                    subject = str(value).strip()
                    break
            details.append(f"{subject}：{reason}" if subject else reason)

        # Per-item results only ever arrive as a list; a mapping/scalar ``data``
        # is a normal payload (e.g. the token response) and is left alone.
        for entry in response.get("data") if isinstance(response.get("data"), list) else []:
            describe(entry)
        error_details = response.get("error_details")
        if isinstance(error_details, list):
            for entry in error_details:
                if isinstance(entry, str) and entry.strip():
                    details.append(entry.strip())
                else:
                    describe(entry)
        return details

    @classmethod
    def _validated_response(cls, response: Mapping[str, Any]) -> Mapping[str, Any]:
        code = response.get("code")
        success = response.get("success")
        succeeded = (code is None or code in {0, "0", 200, "200"}) and success is not False
        if succeeded:
            # A success envelope may still carry per-item rejections. Surfacing
            # them as a failure keeps a rejected order from being recorded as
            # pushed (batch endpoints report those inside ``data``).
            partial = cls.item_failure_details(response)
            if not partial:
                return response
            raise LingxingError(
                f"领星接口部分数据被拒绝：{'；'.join(partial)}",
                code=str(code) if code is not None else None,
            )
        code_str = str(code)
        # A stale-token code must not be retried at the transport level (the
        # token would still be dead); ``push`` re-authenticates and retries it.
        token_invalid = code_str in TOKEN_INVALID_CODES
        retryable = (not token_invalid) and (
            code_str == "103" or (isinstance(code, int) and code >= 500)
        )
        # ``message`` is unhelpful on batch endpoints (it stays "success" even
        # when code is 1), so prefer the per-item details when present.
        details = cls.item_failure_details(response)
        message = (
            "；".join(details)
            if details
            else str(response.get("message") or response.get("msg") or "领星接口请求失败")
        )
        raise LingxingError(
            f"领星接口请求失败（{code}）：{message}",
            retryable=retryable,
            code=code_str,
        )

    def _fetch_token(self, credentials: LingxingCredentials, *, refresh: bool) -> str:
        if refresh:
            query = {
                "refreshToken": self.token_cache.refresh_token,
                "appId": credentials.app_id,
            }
            endpoint = self.endpoints.get("refresh_token", "")
        else:
            query = {
                "appId": credentials.app_id,
                "appSecret": credentials.app_secret,
            }
            endpoint = self.endpoints.get("token", "")
        try:
            response = self._request_with_retry(
                "POST",
                self._endpoint_url(endpoint, query),
                None,
                {},
                token_request=True,
            )
            data = response.get("data") if isinstance(response.get("data"), Mapping) else response
            self.token_cache.update(data, self.clock())
            return self.token_cache.access_token
        except LingxingError:
            if refresh:
                self.token_cache.clear()
                return self._fetch_token(credentials, refresh=False)
            raise


    def access_token(self) -> str:
        credentials = self.credentials()
        now = self.clock()
        if self.token_cache.is_access_valid(now):
            return self.token_cache.access_token
        return self._fetch_token(credentials, refresh=self.token_cache.is_refresh_valid(now))

    def _signed_request(
        self,
        endpoint: str,
        credentials: LingxingCredentials,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        token = self.access_token()
        now = self.clock()
        timestamp = str(int(now.timestamp()))
        sign = compute_sign(
            payload,
            token,
            timestamp,
            self.sign_strategy,
            app_id=credentials.app_id,
        )
        query = {
            "app_key": credentials.app_id,
            "access_token": token,
            "timestamp": timestamp,
            "sign": sign,
        }
        response = self._request_with_retry(
            "POST",
            self._endpoint_url(endpoint, query),
            dict(payload),
            {"Content-Type": "application/json"},
        )
        return sanitize_response(response)

    def push(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        credentials = self.ensure_operation_ready(operation)
        endpoint = self._endpoint_url(self.endpoints.get(operation, ""))
        if not payload:
            raise LingxingError("无法生成请求签名", status=400)
        try:
            return self._signed_request(endpoint, credentials, payload)
        except LingxingError as error:
            # A stale access token: drop the cached token, re-authenticate and
            # retry exactly once (a plain retry would reuse the dead token).
            if error.code in TOKEN_INVALID_CODES:
                self.token_cache.clear()
                return self._signed_request(endpoint, credentials, payload)
            raise
