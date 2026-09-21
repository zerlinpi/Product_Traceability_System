"""Permission matrix characterization tests.

Two layers of protection:

1. **Source-level contract** — pins the route inventory and the known
   authorization deviations. Adding a route, removing a guard, or moving a guard
   between layers fails these tests, forcing a deliberate update of
   ``docs/PERMISSION_MATRIX.md``. Run ``python tools/extract_routes.py --sync``
   after any intentional change.

2. **HTTP behaviour** — exercises the real app with anonymous / ADMIN /
   WAREHOUSE / OPERATIONS principals and asserts the status code each one gets.

Deviations D2/D3/D4 are documented in ``docs/PERMISSION_MATRIX.md``. They are
**pinned here on purpose**: the tests assert the CURRENT (deviating) behaviour so
that fixing them requires an explicit, reviewable test change rather than a
silent behaviour shift.

Feature: batch-traceability, Property 33: 角色互斥不变式（恰好三角色）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from capability_helpers import (  # noqa: E402
    PASSWORD,
    bootstrap_admin,
    insert_user,
    login,
    make_auth_app,
)

from extract_routes import effective, extract  # noqa: E402


# --------------------------------------------------------------------------
# Pinned baseline. Update these together with docs/PERMISSION_MATRIX.md.
# --------------------------------------------------------------------------

EXPECTED_ROUTE_COUNT = 107

# Routes whose ONLY guard is a scope check. Because
# ``auth.current_operator_id()`` returns None, ``require_product_model_access()``
# and ``require_supplier_access()`` always pass — so these routes effectively
# require nothing beyond being logged in.  DEVIATION D2.
EXPECTED_SCOPE_ONLY_ROUTES = {
    ("POST", "/api/batch-trace/query"),
    ("GET", "/api/genealogy"),
    ("GET", "/api/machines/<int:machine_id>/qr"),
    ("GET", "/api/part-label-batches/<int:batch_id>"),
    ("GET", "/api/part-label-batches/<int:batch_id>/qrcodes.zip"),
    ("GET", "/api/part-labels/<int:label_id>/qr"),
    ("GET", "/api/production-batches/<int:batch_id>"),
    ("GET", "/api/production-batches/<int:batch_id>/qr"),
    ("GET", "/api/records"),
    ("DELETE", "/api/records/<int:record_id>"),
    ("PUT", "/api/records/<int:record_id>"),
    ("POST", "/api/scan"),
}

# Routes whose role guard lives inside the service helper they delegate to,
# not in the handler body.  DEVIATION D5.
EXPECTED_SERVICE_GUARDED_ROUTES = {
    ("POST", "/api/batch-trace-records/<int:record_id>/pass"),
    ("PUT", "/api/records/<int:record_id>"),
    ("DELETE", "/api/records/<int:record_id>"),
    ("POST", "/api/purchase-orders/<int:purchase_order_id>/push"),
    ("POST", "/api/production-orders"),
    ("POST", "/api/production-orders/batch"),
}

# Parameter-free routes by effective access level (used for HTTP assertions).
PARAM_FREE_ADMIN = {
    ("GET", "/api/audit-events"),
    ("GET", "/api/dashboard"),
    ("GET", "/api/product-code-batches"),
    ("GET", "/api/settings"),
    ("GET", "/api/supplier-inventory-batches"),
    ("GET", "/api/users"),
    ("POST", "/api/part-types"),
    ("POST", "/api/product-families"),
    ("POST", "/api/product-models"),
    ("POST", "/api/supplier-inventory-batches"),
    ("POST", "/api/suppliers"),
    ("POST", "/api/trace-plans"),
    ("POST", "/api/users"),
    ("PUT", "/api/records/status/bulk"),
    ("PUT", "/api/settings"),
}

PARAM_FREE_OPERATIONS = {
    ("GET", "/api/inventory-sync"),
    ("GET", "/api/lingxing/status"),
    ("GET", "/api/purchase-orders/export"),
    ("POST", "/api/inventory-sync"),
    ("POST", "/api/product-images"),
    ("POST", "/api/products"),
    ("POST", "/api/purchase-orders"),
}

PARAM_FREE_WAREHOUSE = {
    ("GET", "/api/inbound-scan-records"),
    ("GET", "/api/production-orders"),
    ("POST", "/api/inbound-receipts"),
}

PARAM_FREE_ANY_AUTHENTICATED = {
    ("GET", "/api/auth/me"),
    ("GET", "/api/batch-trace-records"),
    ("GET", "/api/bluetooth/status"),
    ("GET", "/api/inbound-receipts"),
    ("GET", "/api/machines"),
    ("GET", "/api/part-label-batches"),
    ("GET", "/api/part-labels"),
    ("GET", "/api/part-types"),
    ("GET", "/api/product-attribute-columns"),
    ("GET", "/api/product-families"),
    ("GET", "/api/product-models"),
    ("GET", "/api/production-batches"),
    ("GET", "/api/products"),
    ("GET", "/api/purchase-orders"),
    ("GET", "/api/records/export.xlsx"),
    ("GET", "/api/scan/session"),
    ("GET", "/api/suppliers"),
    ("GET", "/api/trace-plans"),
    ("POST", "/api/auth/change-password"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/bluetooth/discover"),
    ("POST", "/api/bluetooth/read-sn"),
    ("POST", "/api/scan/reset"),
    ("POST", "/api/scan/undo"),
}

# Scope guards only — no role guard at all, and the scope guard is a no-op.
# Effectively "any authenticated".  DEVIATION D2.
PARAM_FREE_SCOPE_PRODUCT_ONLY = {
    ("GET", "/api/records"),
    ("POST", "/api/batch-trace/query"),
}

PARAM_FREE_SCOPE_BOTH_ONLY = {
    ("GET", "/api/genealogy"),
    ("POST", "/api/scan"),
}

PARAM_FREE_SCOPE_ONLY = PARAM_FREE_SCOPE_PRODUCT_ONLY | PARAM_FREE_SCOPE_BOTH_ONLY

# ADMIN plus a no-op scope guard.
PARAM_FREE_ADMIN_SCOPED = {
    ("POST", "/api/machines"),
    ("POST", "/api/part-labels"),
    ("GET", "/api/product-code-sets"),
}

# ADMIN + WAREHOUSE plus a no-op scope guard. Two of these
# (/api/production-orders and /api/production-orders/batch) get their role guard
# from the service layer.  DEVIATION D5.
PARAM_FREE_WAREHOUSE_SCOPED = {
    ("POST", "/api/batch-entry/scan"),
    ("POST", "/api/production-batches"),
    ("POST", "/api/production-orders"),
    ("POST", "/api/production-orders/batch"),
    ("POST", "/api/scan-gun/inbound"),
    ("POST", "/api/scan-gun/lookup"),
}

# Reachable without logging in: the two /api/ paths exempted in
# auth.before_request, plus every non-/api/ route (the gate only inspects /api/).
PARAM_FREE_PUBLIC = {
    ("GET", "/"),
    ("GET", "/favicon.ico"),
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
}

# Kept for the anonymous-rejection test, which works on string paths.
PUBLIC_PATHS = {"/api/health", "/api/auth/login"}


def _routes() -> list[dict]:
    return extract()


def _keys(route: dict) -> tuple[str, str]:
    return (route["method"], route["path"])


# --------------------------------------------------------------------------
# 1. Source-level contract
# --------------------------------------------------------------------------


def test_route_inventory_is_unchanged():
    routes = _routes()
    assert len(routes) == EXPECTED_ROUTE_COUNT, (
        f"route count changed to {len(routes)}; "
        "update EXPECTED_ROUTE_COUNT and docs/PERMISSION_MATRIX.md "
        "(python tools/extract_routes.py --sync)"
    )


def test_scope_only_routes_are_exactly_the_known_deviation_set():
    routes = _routes()
    actual = {
        _keys(route) for route in routes if not route["roles"] and route["scopes"]
    }
    assert actual == EXPECTED_SCOPE_ONLY_ROUTES, (
        "the set of routes relying on no-op scope guards changed. If intentional, "
        "update EXPECTED_SCOPE_ONLY_ROUTES and DEVIATION D2 in docs/PERMISSION_MATRIX.md"
    )


def test_service_guarded_routes_are_exactly_the_known_set():
    routes = _routes()
    actual = {_keys(route) for route in routes if route["guarded_in_service"]}
    assert actual == EXPECTED_SERVICE_GUARDED_ROUTES, (
        "guards moved between handler and service layer. A permission audit that "
        "only reads handler bodies will now produce wrong answers; update "
        "EXPECTED_SERVICE_GUARDED_ROUTES and DEVIATION D5"
    )


def test_every_param_free_route_is_classified_by_effective_access():
    """The buckets below must exactly cover every parameter-free route."""
    routes = [route for route in _routes() if "<" not in route["path"]]
    actual = {_keys(route) for route in routes}
    pinned = (
        PARAM_FREE_ADMIN
        | PARAM_FREE_OPERATIONS
        | PARAM_FREE_WAREHOUSE
        | PARAM_FREE_ANY_AUTHENTICATED
        | PARAM_FREE_SCOPE_ONLY
        | PARAM_FREE_ADMIN_SCOPED
        | PARAM_FREE_WAREHOUSE_SCOPED
        | PARAM_FREE_PUBLIC
    )
    assert actual == pinned, (
        f"unclassified: {sorted(actual - pinned)}\n"
        f"stale entries: {sorted(pinned - actual)}"
    )


def test_pinned_buckets_do_not_overlap():
    """A route must belong to exactly one access bucket."""
    buckets = {
        "ADMIN": PARAM_FREE_ADMIN,
        "OPERATIONS": PARAM_FREE_OPERATIONS,
        "WAREHOUSE": PARAM_FREE_WAREHOUSE,
        "ANY": PARAM_FREE_ANY_AUTHENTICATED,
        "SCOPE_ONLY": PARAM_FREE_SCOPE_ONLY,
        "ADMIN_SCOPED": PARAM_FREE_ADMIN_SCOPED,
        "WAREHOUSE_SCOPED": PARAM_FREE_WAREHOUSE_SCOPED,
        "PUBLIC": PARAM_FREE_PUBLIC,
    }
    names = list(buckets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            overlap = buckets[left] & buckets[right]
            assert not overlap, f"{left} and {right} both claim {sorted(overlap)}"


def test_effective_access_of_pinned_buckets_matches_source():
    routes = {_keys(route): route for route in _routes()}
    expectations = [
        ("ADMIN", PARAM_FREE_ADMIN),
        ("OPERATIONS", PARAM_FREE_OPERATIONS),
        ("WAREHOUSE", PARAM_FREE_WAREHOUSE),
        ("any authenticated", PARAM_FREE_ANY_AUTHENTICATED),
        ("public (no login)", PARAM_FREE_PUBLIC),
        ("scope:product(NOOP)", PARAM_FREE_SCOPE_PRODUCT_ONLY),
        ("scope:product+scope:supplier(NOOP)", PARAM_FREE_SCOPE_BOTH_ONLY),
        (
            "ADMIN + scope:product(NOOP)",
            {("GET", "/api/product-code-sets"), ("POST", "/api/machines")},
        ),
        ("ADMIN + scope:supplier(NOOP)", {("POST", "/api/part-labels")}),
        ("WAREHOUSE + scope:product(NOOP)", PARAM_FREE_WAREHOUSE_SCOPED),
    ]
    for label, keys in expectations:
        for key in keys:
            assert effective(routes[key]) == label, (
                f"{key[0]} {key[1]}: expected {label!r}, got {effective(routes[key])!r}"
            )


# --------------------------------------------------------------------------
# 2. HTTP behaviour
# --------------------------------------------------------------------------


@pytest.fixture()
def principals(tmp_path):
    """An app with one ADMIN, one WAREHOUSE and one OPERATIONS principal."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    insert_user(database_path, "pm-warehouse", "WAREHOUSE")
    insert_user(database_path, "pm-operations", "OPERATIONS")
    warehouse, warehouse_csrf = login(app, "pm-warehouse")
    operations, operations_csrf = login(app, "pm-operations")
    return {
        "app": app,
        "admin": (admin, admin_csrf),
        "warehouse": (warehouse, warehouse_csrf),
        "operations": (operations, operations_csrf),
    }


def _call(client, csrf, method: str, path: str):
    if method == "GET":
        return client.get(path)
    return client.open(path, method=method, json={}, headers={"X-CSRF-Token": csrf})


def _all_param_free_api_paths() -> list[tuple[str, str]]:
    return sorted(
        key
        for key in (
            PARAM_FREE_ADMIN
            | PARAM_FREE_OPERATIONS
            | PARAM_FREE_WAREHOUSE
            | PARAM_FREE_ANY_AUTHENTICATED
            | PARAM_FREE_SCOPE_ONLY
            | PARAM_FREE_ADMIN_SCOPED
            | PARAM_FREE_WAREHOUSE_SCOPED
        )
        if key[1].startswith("/api/")
    )


@pytest.mark.parametrize("method,path", _all_param_free_api_paths())
def test_anonymous_is_rejected_with_401(principals, method, path):
    """No anonymous access to any /api/ route outside PUBLIC_PATHS."""
    assert path not in PUBLIC_PATHS
    response = _call(principals["app"].test_client(), "", method, path)
    assert response.status_code == 401, (
        f"{method} {path} answered {response.status_code} anonymously; "
        "every /api/ route except /api/health and /api/auth/login must require login"
    )


GATE_REJECTION_MESSAGE = "登录状态已失效，请重新登录"


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_PUBLIC))
def test_public_routes_do_not_require_login(principals, method, path):
    """Only 4 routes bypass the authentication gate — keep that set small.

    ``POST /api/auth/login`` legitimately answers 401 for bad credentials, so
    the assertion is on the gate's own message rather than the status code.
    """
    response = _call(principals["app"].test_client(), "", method, path)
    body = response.get_json(silent=True) or {}
    assert body.get("message") != GATE_REJECTION_MESSAGE, (
        f"{method} {path} is now behind the session gate; if intentional, remove "
        "it from PARAM_FREE_PUBLIC and update docs/PERMISSION_MATRIX.md"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_ADMIN))
@pytest.mark.parametrize("role", ["warehouse", "operations"])
def test_admin_only_routes_reject_other_roles_with_403(principals, role, method, path):
    client, csrf = principals[role]
    response = _call(client, csrf, method, path)
    assert response.status_code == 403, (
        f"{role} got {response.status_code} on ADMIN-only {method} {path}"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_OPERATIONS))
def test_operations_only_routes_reject_warehouse_with_403(principals, method, path):
    client, csrf = principals["warehouse"]
    response = _call(client, csrf, method, path)
    assert response.status_code == 403, (
        f"warehouse got {response.status_code} on OPERATIONS route {method} {path}"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_WAREHOUSE))
def test_warehouse_only_routes_reject_operations_with_403(principals, method, path):
    client, csrf = principals["operations"]
    response = _call(client, csrf, method, path)
    assert response.status_code == 403, (
        f"operations got {response.status_code} on WAREHOUSE route {method} {path}"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_ANY_AUTHENTICATED))
@pytest.mark.parametrize("role", ["admin", "warehouse", "operations"])
def test_any_authenticated_routes_admit_every_role(principals, role, method, path):
    """DEVIATION D2/D6: these carry no role guard at all.

    They are asserted to admit all three roles. A 401/403 here would mean a
    guard appeared — good, but update the matrix first.
    """
    client, csrf = principals[role]
    response = _call(client, csrf, method, path)
    assert response.status_code not in {401, 403}, (
        f"{role} was denied on {method} {path} ({response.status_code}); "
        "a role guard was added — update docs/PERMISSION_MATRIX.md"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_SCOPE_ONLY))
@pytest.mark.parametrize("role", ["warehouse", "operations"])
def test_scope_only_routes_admit_non_admin_roles(principals, role, method, path):
    """DEVIATION D2 pinned: scope-only routes admit WAREHOUSE and OPERATIONS."""
    client, csrf = principals[role]
    response = _call(client, csrf, method, path)
    assert response.status_code not in {401, 403}, (
        f"{role} was denied on scope-only {method} {path} ({response.status_code}); "
        "scope enforcement was re-enabled — update DEVIATION D2"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_ADMIN_SCOPED))
@pytest.mark.parametrize("role", ["warehouse", "operations"])
def test_admin_scoped_routes_reject_non_admin_roles(principals, role, method, path):
    """The role guard is real even though the paired scope guard is a no-op."""
    client, csrf = principals[role]
    response = _call(client, csrf, method, path)
    assert response.status_code == 403, (
        f"{role} got {response.status_code} on ADMIN route {method} {path}"
    )


@pytest.mark.parametrize("method,path", sorted(PARAM_FREE_WAREHOUSE_SCOPED))
def test_warehouse_scoped_routes_reject_operations(principals, method, path):
    """Covers the two service-layer-guarded production-order routes (D5)."""
    client, csrf = principals["operations"]
    response = _call(client, csrf, method, path)
    assert response.status_code == 403, (
        f"operations got {response.status_code} on WAREHOUSE route {method} {path}"
    )


# --------------------------------------------------------------------------
# 3. Deviations pinned on purpose
# --------------------------------------------------------------------------


def test_deviation_d5_service_layer_guard_is_effective(principals):
    """``/pass`` has no guard in its handler, but the service layer enforces ADMIN.

    This is the counter-example that makes a handler-only audit wrong.
    """
    client, csrf = principals["warehouse"]
    response = client.post(
        "/api/batch-trace-records/999999/pass", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403, (
        "warehouse must be denied by require_admin() inside transition_batch_quality"
    )


def test_deviation_d2_scope_only_write_is_reachable_by_operations(principals):
    """DEVIATION D2: ``POST /api/scan`` is guarded only by no-op scope checks."""
    client, csrf = principals["operations"]
    response = client.post("/api/scan", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code != 403, (
        "operations reached a scope-only write route; if a real guard was added, "
        "remove this deviation pin and update docs/PERMISSION_MATRIX.md D2"
    )


def test_deviation_d3_record_edit_has_no_role_guard(principals):
    """DEVIATION D3: ``PUT /api/records/<id>`` has no effective role guard.

    ``editable_record()`` contains an owner check, but it is dead code because
    ``current_operator_id()`` always returns None. A missing record therefore
    yields 404 (not 403) for OPERATIONS — proving no role check runs first.
    """
    client, csrf = principals["operations"]
    response = client.put(
        "/api/records/999999",
        json={"remarks": "校对", "partCodes": ["NOT-A-REAL-CODE"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404, (
        f"expected 404 (no role guard, lookup first), got {response.status_code}. "
        "If a guard was added, update DEVIATION D3 in docs/PERMISSION_MATRIX.md"
    )


def test_deviation_d4_scope_tables_do_not_gate_access(principals, tmp_path):
    """DEVIATION D4: scope rows are stored and echoed but grant nothing.

    A WAREHOUSE user with NO product scope row still reaches a product-scoped
    route, because the scope guard short-circuits on ``current_operator_id() is None``.
    """
    client, csrf = principals["warehouse"]
    response = client.get("/api/records")
    assert response.status_code != 403, (
        "warehouse was denied a scope-guarded route; scope enforcement was "
        "re-enabled — update DEVIATION D2/D4 in docs/PERMISSION_MATRIX.md"
    )


def test_scope_ids_are_still_echoed_by_the_api(principals):
    """DEVIATION D4 (API surface): user payloads still advertise scope ids."""
    client, csrf = principals["admin"]
    created = client.post(
        "/api/users",
        json={
            "username": "pm-scoped",
            "displayName": "范围仓管",
            "password": PASSWORD,
            "role": "WAREHOUSE",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.get_json()
    data = created.get_json()["data"]
    for field in ("productModels", "productModelIds", "suppliers", "supplierIds"):
        assert field in data, (
            f"{field} disappeared from the user payload; the API contract in "
            "docs/API_CONTRACT.md §8 promises these fields stay for compatibility"
        )


def test_role_guards_are_exactly_three_distinct_behaviours():
    """DEVIATION D1: require_warehouse() and require_admin_or_warehouse() are identical."""
    from traceability import auth

    assert auth.VALID_ROLES == {"ADMIN", "WAREHOUSE", "OPERATIONS"}

    def denies(guard, role):
        class _User(dict):
            pass

        original = auth.current_user
        auth.current_user = lambda: {"id": 1, "role": role, "display_name": role}
        try:
            guard()
            return False
        except auth.AuthError:
            return True
        finally:
            auth.current_user = original

    for role in ("ADMIN", "WAREHOUSE", "OPERATIONS"):
        assert denies(auth.require_warehouse, role) == denies(
            auth.require_admin_or_warehouse, role
        ), "D1 no longer holds: the two guards diverged — update the matrix"
