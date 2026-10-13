"""Smoke-test the running service over real HTTP.

Why this exists
---------------
Every other check in this repository drives Flask's test client, which never
opens a socket. That is the right way to test behaviour, and it is also why a
whole class of things goes unverified: cookies as the browser actually sends
them, response headers as the deployment actually sets them, gzip negotiation,
``Cache-Control`` on the bundle, and the CSRF requirement seen from outside
rather than from inside a request context.

It is also how the logout bug was found. ``session.clear()`` looked correct in
every unit test — the client it was called on really was logged out — while a
copy of the cookie kept working, because nothing in the suite ever held one.

What it is not
--------------
Not a replacement for the suites, and not a business test: ``tools/acceptance.py``
covers the workflow. This checks that the service is reachable, that it refuses
what it should refuse, and that the headers a browser depends on are the ones
being sent.

    python tools/smoke.py
    python tools/smoke.py --port 5099     # pick a different port

Exit code is 0 only when every check passed. The server it starts is
``tools/dev_server.py``, so it uses a throwaway database and never touches
``data/``.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import dev_server  # noqa: E402

#: The session cookie Flask signs. Captured by name so the logout check can replay it.
SESSION_COOKIE = "session"

#: Sent on every request. Loopback must not go through a system proxy: on a
#: machine behind one, an unproxied-by-accident request to 127.0.0.1 either
#: fails or is answered by the proxy instead of by this server.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class Results:
    """Collects check outcomes so the run ends with one honest summary."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def check(self, section: str, name: str, condition: bool, detail: str = "", hint: str = "") -> None:
        """Record one check.

        ``detail`` is printed either way — it is the measurement. ``hint`` is
        printed only on failure, because a passing check explaining how it would
        have failed reads as a warning.
        """
        self.rows.append((section, name, "通过" if condition else "失败"))
        mark = "✓" if condition else "✗"
        suffix = f"  — {detail}" if detail else ""
        if not condition and hint:
            suffix = f"  — {hint}"
        print(f"  {mark} {name}{suffix}")

    def failed(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[2] == "失败"]

    def summary(self) -> str:
        passed = len(self.rows) - len(self.failed())
        return f"{passed}/{len(self.rows)} 项通过"


class Client:
    """A browser-shaped client: keeps cookies, sends the CSRF token, reads headers."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.csrf = ""

    def request(self, method: str, path: str, *, body=None, headers=None, follow=True):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = self.opener.open(request)
        except urllib.error.HTTPError as error:  # 4xx/5xx are answers, not failures
            return error
        return response

    def status(self, method: str, path: str, **kwargs) -> int:
        response = self.request(method, path, **kwargs)
        response.read()
        return response.status

    def json(self, method: str, path: str, **kwargs):
        response = self.request(method, path, **kwargs)
        return json.loads(response.read())

    def write(self, method: str, path: str, body=None, *, csrf: str | None = None):
        """A state-changing call, with the CSRF header unless one is withheld."""
        headers = {} if csrf is None else {"X-CSRF-Token": csrf}
        return self.status(method, path, body=body, headers=headers)

    def session_value(self) -> str:
        """The raw signed session cookie, as a copy taken from the browser would be."""
        for cookie in self.jar:
            if cookie.name == SESSION_COOKIE:
                return cookie.value
        return ""


def login(base: str, *, password: str) -> Client:
    client = Client(base)
    response = client.request(
        "POST", "/api/auth/login", body={"username": "admin", "password": password}
    )
    payload = json.loads(response.read())
    assert payload.get("ok"), payload
    client.csrf = payload["data"]["csrfToken"]
    return client


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_reachability(base: str, results: Results) -> None:
    print("\n【1】服务可达性")
    anonymous = Client(base)

    results.check("1", "GET /api/health", anonymous.status("GET", "/api/health") == 200)
    results.check("1", "GET / 返回 SPA 入口", anonymous.status("GET", "/") == 200)
    results.check(
        "1",
        "静态资源无需登录",
        anonymous.status("GET", "/static/dist/favicon.svg") == 200,
    )
    # The guard runs before routing, so an unknown /api path is a 401 rather than
    # a 404. Asserted because it is the kind of thing a refactor flips silently.
    results.check(
        "1",
        "未登录访问 /api/* 一律 401（不泄露路由表）",
        anonymous.status("GET", "/api/products") == 401
        and anonymous.status("GET", "/api/definitely-not-a-route") == 401,
    )


def check_auth(base: str, results: Results) -> None:
    print("\n【2】认证与 CSRF")
    anonymous = Client(base)

    results.check(
        "2",
        "错误密码被拒",
        anonymous.status(
            "POST", "/api/auth/login", body={"username": "admin", "password": "definitely-wrong"}
        )
        == 401,
    )

    client = login(base, password=dev_server.ADMIN_PASSWORD)
    me = client.json("GET", "/api/auth/me")
    results.check("2", "登录后可读当前用户", me["data"]["username"] == "admin")
    results.check("2", "角色为 ADMIN", me["data"]["role"] == "ADMIN")
    results.check("2", "登录下发了会话 cookie", bool(client.session_value()))

    results.check(
        "2",
        "写请求缺 CSRF 被拒",
        client.write("POST", "/api/products", {}, csrf=None) == 403,
    )
    results.check(
        "2",
        "写请求 CSRF 错误被拒",
        client.write("POST", "/api/products", {}, csrf="bogus-token") == 403,
    )


def check_reads(base: str, results: Results, client: Client) -> None:
    print("\n【3】基础资料与业务只读端点")
    for path in (
        "/api/products",
        "/api/suppliers",
        "/api/product-models",
        "/api/product-families",
        "/api/part-types",
        "/api/machines",
        "/api/trace-plans",
        "/api/part-labels",
        "/api/part-label-batches",
        "/api/product-attribute-columns",
        "/api/bluetooth/status",
        "/api/users",
        "/api/settings",
        "/api/dashboard",
        "/api/inventory-sync",
        "/api/purchase-orders",
        "/api/inbound-receipts",
        "/api/production-batches",
        "/api/batch-trace-records",
    ):
        status = client.status("GET", path)
        results.check("3", f"GET {path}", status == 200, f"HTTP {status}")

    # A scanner session only exists while a scan gun is mid-task; without one the
    # honest answer is 400, not a 500 and not an empty 200.
    status = client.status("GET", "/api/scan/session")
    results.check("3", "GET /api/scan/session 无会话时 400", status == 400, f"HTTP {status}")


def check_list_window(base: str, results: Results, client: Client) -> None:
    print("\n【4】列表窗口响应头")
    response = client.request("GET", "/api/purchase-orders")
    headers = response.headers
    response.read()
    for header in ("X-Total-Count", "X-Returned-Count", "X-List-Limit"):
        results.check("4", f"{header} 存在", headers.get(header) is not None)

    print("\n【5】安全响应头")
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
    ):
        results.check("5", f"{header} 已设置", headers.get(header) is not None)


def check_static_delivery(base: str, results: Results, client: Client) -> None:
    print("\n【6】静态资源交付")
    page = client.request("GET", "/").read().decode("utf-8", "replace")
    match = re.search(r"/static/dist/assets/[A-Za-z0-9._-]+\.js", page)
    if not match:
        results.check("6", "在首页找到内容哈希资源", False, "未找到 /assets/*.js")
        return
    asset = match.group(0)

    results.check("6", "哈希资源无需登录", Client(base).status("GET", asset) == 200)

    plain = Client(base).request("GET", asset)
    plain.read()
    results.check(
        "6",
        "不带 Accept-Encoding 时不压缩",
        plain.headers.get("Content-Encoding") is None,
    )
    results.check(
        "6",
        "哈希资源可长期缓存",
        "immutable" in (plain.headers.get("Cache-Control") or ""),
    )

    compressed = Client(base).request("GET", asset, headers={"Accept-Encoding": "gzip"})
    compressed.read()
    results.check(
        "6",
        "带 Accept-Encoding 时发送预压缩副本",
        compressed.headers.get("Content-Encoding") == "gzip",
    )


def check_logout(base: str, results: Results) -> None:
    """The regression this tool was written for.

    ``session.clear()`` logs the browser out and leaves every copy of the cookie
    valid, which is invisible to any test that only ever holds one client.
    """
    print("\n【7】登出")
    donor = login(base, password=dev_server.ADMIN_PASSWORD)
    captured = donor.session_value()

    client = login(base, password=dev_server.ADMIN_PASSWORD)
    results.check(
        "7",
        "退出登录",
        client.write("POST", "/api/auth/logout", csrf=client.csrf) == 200,
    )
    results.check("7", "登出后本客户端失效", client.status("GET", "/api/auth/me") == 401)

    replay = Client(base)
    replay.jar.set_cookie(
        http.cookiejar.Cookie(
            version=0,
            name=SESSION_COOKIE,
            value=captured,
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )
    )
    results.check(
        "7",
        "登出后旧 cookie 不能读",
        replay.status("GET", "/api/auth/me") == 401,
        hint="签名 cookie 未作废——session_version 没有递增",
    )
    results.check(
        "7",
        "登出后旧 cookie 不能写",
        replay.write("POST", "/api/products", {}) == 401,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5099, help="port for the dev server")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "dev_server.py"), "--port", str(args.port)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    results = Results()
    try:
        for _ in range(60):
            if server.poll() is not None:
                print("服务未能启动，输出如下：", file=sys.stderr)
                print(server.stdout.read() if server.stdout else "", file=sys.stderr)
                return 1
            try:
                if _OPENER.open(base + "/api/health", timeout=2).status == 200:
                    break
            except Exception:  # noqa: BLE001 — not up yet
                time.sleep(1)

        print(f"服务已就绪：{base}")
        check_reachability(base, results)
        check_auth(base, results)
        # One logged-in client for the read checks, so the login cost is paid once.
        client = login(base, password=dev_server.ADMIN_PASSWORD)
        check_reads(base, results, client)
        check_list_window(base, results, client)
        check_static_delivery(base, results, client)
        check_logout(base, results)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            server.kill()

    print(f"\n=== 冒烟结果：{results.summary()} ===")
    failures = results.failed()
    if failures:
        print("失败项：")
        for section, name, _ in failures:
            print(f"  [{section}] {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
