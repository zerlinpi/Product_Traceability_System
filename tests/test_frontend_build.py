"""The built frontend and its contract with the backend.

The UI is built from ``frontend/`` into ``static/dist`` and committed, so the
Python suite can check — without Node.js — that:

- the shipped entry page is compatible with the server's strict CSP, and the
  one inline ``<style>`` a library renders is exactly the one the CSP allows;
- Flask serves the entry uncached and the hashed bundles as immutable;
- every endpoint the UI calls exists with that HTTP method;
- every page a role can open can actually load its data as that role — the
  navigation must never offer a page the capability matrix would refuse;
- the idempotency scopes the UI sends are the ones the server implements.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

from app import create_app
from capability_helpers import bootstrap_admin, insert_user, login, make_auth_app
from frontend_sources import (
    DIST,
    DIST_INDEX,
    ROOT,
    SRC,
    VIEWS,
    api_calls,
    menu_pages,
    pages,
    read,
    source_files,
)
from traceability.responses import CONTENT_SECURITY_POLICY, REKA_SCROLL_AREA_STYLE_HASH

# The fixed style text reka-ui's ScrollAreaViewport renders as a <style> element.
REKA_STYLE_MARKER = "[data-reka-scroll-area-viewport] { scrollbar-width:none;"


def _bundle_text() -> str:
    return "\n".join(read(path) for path in source_files(".js", base=DIST / "assets"))


# --------------------------------------------------------------------------- #
# The shipped entry page and bundle
# --------------------------------------------------------------------------- #
def test_built_entry_page_is_csp_safe():
    html = read(DIST_INDEX)
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S):
        assert "src=" in match.group(1) and not match.group(2).strip(), "inline <script> would be blocked"
    assert "<style" not in html, "inline <style> would be blocked"
    assert not re.search(r"\sstyle\s*=", html), "style attributes would be blocked"
    assert not re.search(r"https?://", html), "no third-party origin"
    assert re.search(r'<script type="module" crossorigin src="/static/dist/assets/index-[\w-]+\.js">', html)


def test_csp_allows_exactly_the_library_style_element():
    """reka-ui renders one constant <style>; its hash must match the CSP.

    Recomputed from the committed bundle, so upgrading the library (which may
    change the text) fails here instead of silently breaking the sidebars.
    """
    bundle = _bundle_text()
    start = bundle.find(" /* Hide scrollbars cross-browser")
    assert start != -1, "reka-ui scroll-area style text not found in the bundle"
    quote = bundle[start - 1]  # the minifier may emit "…", '…' or `…`
    assert quote in "\"'`"
    text = bundle[start : bundle.index(quote, start)]
    assert REKA_STYLE_MARKER in text
    digest = base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" == REKA_SCROLL_AREA_STYLE_HASH
    assert f"style-src 'self' {REKA_SCROLL_AREA_STYLE_HASH};" in CONTENT_SECURITY_POLICY
    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in CONTENT_SECURITY_POLICY


def test_bundle_fetches_nothing_from_other_origins():
    """Icons are compiled into CSS; nothing is loaded from a CDN at runtime."""
    css = "\n".join(read(path) for path in source_files(".css", base=DIST / "assets"))
    assert not re.search(r"url\(\s*['\"]?https?://", css)
    assert "@import" not in css
    sources = "\n".join(read(path) for path in source_files(".vue", ".ts", base=SRC))
    # Icons are UnoCSS classes (i-collection:name); online Iconify names are not used.
    for name in re.findall(r'<FaIcon[^>]*\sname="([^"]+)"', sources):
        assert name.startswith("i-"), f"icon {name!r} would be fetched from the Iconify API"


# --------------------------------------------------------------------------- #
# How Flask serves it
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path: Path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "traceability.db")})
    return app.test_client()


def test_entry_is_revalidated_and_carries_the_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    etag = response.headers.get("ETag")
    assert etag
    assert client.get("/", headers={"If-None-Match": etag}).status_code == 304


def test_hashed_bundles_are_cached_forever_and_api_never(client):
    html = client.get("/").get_data(as_text=True)
    script = re.search(r'src="(/static/dist/assets/[^"]+\.js)"', html).group(1)
    asset = client.get(script)
    assert asset.status_code == 200
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert asset.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    asset.close()
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"


def test_missing_build_is_reported_instead_of_a_blank_page(tmp_path: Path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "traceability.db")})
    from traceability.responses import frontend_entry_response

    with app.test_request_context("/"):
        response = frontend_entry_response("static/dist/does-not-exist.html")
    assert response.status_code == 503
    assert "pnpm build" in response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Frontend ↔ backend contract
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def url_adapter(tmp_path_factory):
    database = tmp_path_factory.mktemp("url-map") / "traceability.db"
    app = create_app({"TESTING": True, "DATABASE": str(database)})
    return app.url_map.bind("localhost")


CALLS = api_calls()


def test_api_modules_are_parsed():
    """Guard for the parser below: an empty list would make the contract vacuous."""
    assert len(CALLS) >= 50
    assert ("PUT", "/api/records/status/bulk", "records") in CALLS


@pytest.mark.parametrize(("method", "path", "module"), CALLS, ids=[f"{m} {p}" for m, p, _ in CALLS])
def test_every_frontend_call_has_a_backend_route(url_adapter, method, path, module):
    try:
        url_adapter.match(path, method=method)
    except MethodNotAllowed:  # pragma: no cover - reported below
        pytest.fail(f"{module}.ts calls {method} {path}, which the route does not accept")
    except NotFound:  # pragma: no cover - reported below
        pytest.fail(f"{module}.ts calls {method} {path}, which does not exist")


def _backend_idempotent_routes() -> dict[str, str]:
    routes: dict[str, str] = {}
    files = [ROOT / "app.py", *sorted((ROOT / "traceability" / "api").glob("*.py"))]
    pattern = re.compile(
        r'@\w+\.post\("([^"]+)"\)\s*\n\s*def \w+\([^)]*\):\s*\n\s*return run_idempotent\("([^"]+)"'
    )
    for path in files:
        for route, scope in pattern.findall(read(path)):
            routes[scope] = route
    return routes


def test_idempotency_scopes_match_the_server():
    backend = _backend_idempotent_routes()
    frontend: dict[str, str] = {}
    for path in source_files(".ts", base=SRC / "api" / "modules"):
        for route, scope in re.findall(r"api\.post<[^>]*>\('([^']+)',[^\n]*idempotent: '([^']+)'", read(path)):
            frontend[scope] = route
    assert frontend, "no idempotent calls found in the API modules"
    for scope, route in frontend.items():
        assert backend.get(scope) == route, f"{scope}: UI posts {route}, server implements {backend.get(scope)}"
    # Every field-facing write the UI performs is idempotent.
    assert set(frontend) >= {
        "production-batches.create",
        "batch-entry.scan",
        "purchase-orders.create",
        "production-orders.batch",
        "scan-gun.inbound",
    }


# --------------------------------------------------------------------------- #
# Route table sanity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", sorted(pages()))
def test_page_is_consistent_with_the_navigation(key):
    page = pages()[key]
    assert page["roles"], f"{key} declares no roles"
    for view in page["views"]:
        assert (VIEWS / view).is_file(), f"{key}: missing view {view}"
    roles_with_menu = {role for role, keys in menu_pages().items() if key in keys}
    assert roles_with_menu == set(page["roles"]), (
        f"{key}: page roles {page['roles']} differ from the roles whose menu shows it {sorted(roles_with_menu)}"
    )


# --------------------------------------------------------------------------- #
# Every page a role can open can load its data as that role
# --------------------------------------------------------------------------- #
PAGE_ENDPOINTS: dict[str, list[tuple[str, str]]] = {
    "dashboard": [("GET", "/api/dashboard"), ("GET", "/api/production-batches")],
    "products": [("GET", "/api/products"), ("GET", "/api/product-attribute-columns")],
    "suppliers": [("GET", "/api/suppliers"), ("GET", "/api/part-types"), ("GET", "/api/supplier-inventory-batches")],
    "users": [("GET", "/api/users")],
    "batch-gen": [("GET", "/api/production-batches"), ("GET", "/api/products")],
    "batch-entry": [("POST", "/api/batch-entry/scan")],
    "batch-trace": [("POST", "/api/batch-trace/query")],
    "batch-quality": [("GET", "/api/batch-trace-records")],
    "trace": [("GET", "/api/production-batches"), ("GET", "/api/products")],
    "my-records": [("GET", "/api/batch-trace-records")],
    "purchase-orders": [
        ("GET", "/api/purchase-orders"),
        ("GET", "/api/lingxing/status"),
        ("GET", "/api/inbound-receipts"),
        ("GET", "/api/products"),
    ],
    "inbound-receipts": [("GET", "/api/purchase-orders"), ("GET", "/api/inbound-receipts")],
    "production-orders": [("GET", "/api/purchase-orders"), ("GET", "/api/production-orders")],
    "scan-gun": [("GET", "/api/inbound-scan-records"), ("POST", "/api/scan-gun/lookup")],
    "inventory-sync": [("GET", "/api/inventory-sync")],
    "settings": [("GET", "/api/settings")],
}

ROLE_PAGES = [(role, key) for role, keys in sorted(menu_pages().items()) for key in keys]


def test_every_page_declares_its_data_endpoints():
    assert set(PAGE_ENDPOINTS) == set(pages())


@pytest.fixture(scope="module")
def role_clients(tmp_path_factory):
    app, database_path, _fake = make_auth_app(tmp_path_factory.mktemp("role-pages"))
    clients = {"ADMIN": bootstrap_admin(app)}
    for username, role in (("page.warehouse", "WAREHOUSE"), ("page.operations", "OPERATIONS")):
        insert_user(database_path, username, role)
        clients[role] = login(app, username)
    return clients


@pytest.mark.parametrize(("role", "key"), ROLE_PAGES, ids=[f"{role}-{key}" for role, key in ROLE_PAGES])
def test_role_can_load_every_page_in_its_navigation(role_clients, role, key):
    client, csrf = role_clients[role]
    for method, path in PAGE_ENDPOINTS[key]:
        if method == "GET":
            response = client.get(path)
            assert response.status_code == 200, f"{role} {key}: GET {path} -> {response.status_code} {response.get_json()}"
        else:
            # An empty body is rejected as invalid input — but never as unauthorised.
            response = client.post(path, json={}, headers={"X-CSRF-Token": csrf})
            assert response.status_code not in {401, 403, 404, 405} or (
                response.status_code == 404 and response.get_json()["message"] != "接口不存在"
            ), f"{role} {key}: POST {path} -> {response.status_code} {response.get_json()}"
            assert response.status_code < 500
