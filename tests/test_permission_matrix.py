"""Permission matrix characterization tests.

Three layers of protection:

1. **Source-level contract** — pins the route inventory and each route's
   effective role set. Adding a route, removing a guard, or moving a guard
   between layers fails these tests, forcing a deliberate update of
   ``docs/PERMISSION_MATRIX.md``. Run ``python tools/extract_routes.py --sync``
   after any intentional change.

2. **Capability table invariants** — the policy in
   ``traceability/capabilities.py`` must stay coherent.

3. **HTTP behaviour** — exercises the real app with anonymous / ADMIN /
   WAREHOUSE / OPERATIONS principals and asserts the status code each one gets.

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

from extract_routes import extract  # noqa: E402

from traceability.capabilities import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_CAPABILITIES,
    ROLE_OPERATIONS,
    ROLE_WAREHOUSE,
    VALID_ROLES,
    Capability,
    capabilities_for_role,
    roles_for,
)

# --------------------------------------------------------------------------
# Pinned baseline. Update together with docs/PERMISSION_MATRIX.md.
# --------------------------------------------------------------------------

EXPECTED_ROUTE_COUNT = 107

ADMIN = frozenset({ROLE_ADMIN})
ADMIN_WAREHOUSE = frozenset({ROLE_ADMIN, ROLE_WAREHOUSE})
ADMIN_OPERATIONS = frozenset({ROLE_ADMIN, ROLE_OPERATIONS})
EVERY_ROLE = frozenset({ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_OPERATIONS})

# Parameter-free routes that require a login, mapped to the roles that pass.
# Routes carrying a no-op scope guard are included; the scope guard changes
# nothing, which is exactly the point of DEVIATION D2.
PARAM_FREE_ROLES: dict[tuple[str, str], frozenset[str]] = {
    # --- ADMIN only ---
    ("GET", "/api/audit-events"): ADMIN,
    ("GET", "/api/dashboard"): ADMIN,
    ("GET", "/api/product-code-batches"): ADMIN,
    ("GET", "/api/product-code-sets"): ADMIN,
    ("GET", "/api/settings"): ADMIN,
    ("GET", "/api/supplier-inventory-batches"): ADMIN,
    ("GET", "/api/users"): ADMIN,
    ("POST", "/api/machines"): ADMIN,
    ("POST", "/api/part-labels"): ADMIN,
    ("POST", "/api/part-types"): ADMIN,
    ("POST", "/api/product-families"): ADMIN,
    ("POST", "/api/product-models"): ADMIN,
    ("POST", "/api/supplier-inventory-batches"): ADMIN,
    ("POST", "/api/suppliers"): ADMIN,
    ("POST", "/api/trace-plans"): ADMIN,
    ("POST", "/api/users"): ADMIN,
    ("PUT", "/api/records/status/bulk"): ADMIN,
    ("PUT", "/api/settings"): ADMIN,
    # --- ADMIN + WAREHOUSE ---
    ("GET", "/api/inbound-scan-records"): ADMIN_WAREHOUSE,
    ("GET", "/api/production-orders"): ADMIN_WAREHOUSE,
    ("GET", "/api/records"): ADMIN_WAREHOUSE,
    ("POST", "/api/batch-entry/scan"): ADMIN_WAREHOUSE,
    ("POST", "/api/inbound-receipts"): ADMIN_WAREHOUSE,
    ("POST", "/api/production-batches"): ADMIN_WAREHOUSE,
    ("POST", "/api/production-orders"): ADMIN_WAREHOUSE,
    ("POST", "/api/production-orders/batch"): ADMIN_WAREHOUSE,
    ("POST", "/api/scan"): ADMIN_WAREHOUSE,
    ("POST", "/api/scan-gun/inbound"): ADMIN_WAREHOUSE,
    ("POST", "/api/scan-gun/lookup"): ADMIN_WAREHOUSE,
    # --- ADMIN + OPERATIONS ---
    ("GET", "/api/inventory-sync"): ADMIN_OPERATIONS,
    ("GET", "/api/lingxing/status"): ADMIN_OPERATIONS,
    ("GET", "/api/purchase-orders/export"): ADMIN_OPERATIONS,
    ("POST", "/api/inventory-sync"): ADMIN_OPERATIONS,
    ("POST", "/api/product-images"): ADMIN_OPERATIONS,
    ("POST", "/api/products"): ADMIN_OPERATIONS,
    ("POST", "/api/purchase-orders"): ADMIN_OPERATIONS,
    # --- every authenticated role ---
    ("GET", "/api/auth/me"): EVERY_ROLE,
    ("GET", "/api/batch-trace-records"): EVERY_ROLE,
    ("GET", "/api/bluetooth/status"): EVERY_ROLE,
    ("GET", "/api/genealogy"): EVERY_ROLE,
    ("GET", "/api/inbound-receipts"): EVERY_ROLE,
    ("GET", "/api/machines"): EVERY_ROLE,
    ("GET", "/api/part-label-batches"): EVERY_ROLE,
    ("GET", "/api/part-labels"): EVERY_ROLE,
    ("GET", "/api/part-types"): EVERY_ROLE,
    ("GET", "/api/product-attribute-columns"): EVERY_ROLE,
    ("GET", "/api/product-families"): EVERY_ROLE,
    ("GET", "/api/product-models"): EVERY_ROLE,
    ("GET", "/api/production-batches"): EVERY_ROLE,
    ("GET", "/api/products"): EVERY_ROLE,
    ("GET", "/api/purchase-orders"): EVERY_ROLE,
    ("GET", "/api/records/export.xlsx"): EVERY_ROLE,
    ("GET", "/api/scan/session"): EVERY_ROLE,
    ("GET", "/api/suppliers"): EVERY_ROLE,
    ("GET", "/api/trace-plans"): EVERY_ROLE,
    ("POST", "/api/auth/change-password"): EVERY_ROLE,
    ("POST", "/api/auth/logout"): EVERY_ROLE,
    ("POST", "/api/batch-trace/query"): EVERY_ROLE,
    ("POST", "/api/bluetooth/discover"): EVERY_ROLE,
    ("POST", "/api/bluetooth/read-sn"): EVERY_ROLE,
    ("POST", "/api/scan/reset"): EVERY_ROLE,
    ("POST", "/api/scan/undo"): EVERY_ROLE,
}

# Reachable without logging in: the two /api/ paths exempted in
# auth.before_request, plus every non-/api/ route (the gate only inspects /api/).
PARAM_FREE_PUBLIC = {
    ("GET", "/"),
    ("GET", "/favicon.ico"),
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
}

PUBLIC_PATHS = {"/api/health", "/api/auth/login"}
GATE_REJECTION_MESSAGE = "登录状态已失效，请重新登录"

# Routes whose role guard lives inside the helper they delegate to, not in the
# handler body.  DEVIATION D5, plus the idempotent-write split where the handler
# is a thin `run_idempotent(...)` shim and the guard sits in the producer.
EXPECTED_SERVICE_GUARDED_ROUTES = {
    ("POST", "/api/batch-entry/scan"),
    ("POST", "/api/batch-trace-records/<int:record_id>/pass"),
    ("POST", "/api/production-batches"),
    ("POST", "/api/production-orders"),
    ("POST", "/api/production-orders/batch"),
    ("POST", "/api/purchase-orders"),
    ("POST", "/api/purchase-orders/<int:purchase_order_id>/push"),
    ("DELETE", "/api/records/<int:record_id>"),
    ("PUT", "/api/records/<int:record_id>"),
    ("POST", "/api/scan-gun/inbound"),
}

# Write endpoints wrapped in the idempotency layer, mapped to their scope.
IDEMPOTENT_WRITE_ROUTES = {
    ("POST", "/api/scan-gun/inbound"): "scan-gun.inbound",
    ("POST", "/api/batch-entry/scan"): "batch-entry.scan",
    ("POST", "/api/production-batches"): "production-batches.create",
    ("POST", "/api/purchase-orders"): "purchase-orders.create",
    ("POST", "/api/production-orders"): "production-orders.create",
    ("POST", "/api/production-orders/batch"): "production-orders.batch",
}

# Routes that used to be protected only by a no-op scope guard and now carry an
# explicit capability. This set must never grow back into "scope guard only".
CAPABILITY_GUARDED_ROUTES = {
    ("POST", "/api/batch-trace/query"): "TRACE_VIEW",
    ("GET", "/api/genealogy"): "TRACE_VIEW",
    ("GET", "/api/machines/<int:machine_id>/qr"): "TRACE_VIEW",
    ("GET", "/api/part-label-batches/<int:batch_id>"): "TRACE_VIEW",
    ("GET", "/api/part-label-batches/<int:batch_id>/qrcodes.zip"): "TRACE_VIEW",
    ("GET", "/api/part-labels/<int:label_id>/qr"): "TRACE_VIEW",
    ("GET", "/api/production-batches/<int:batch_id>"): "TRACE_VIEW",
    ("GET", "/api/production-batches/<int:batch_id>/qr"): "TRACE_VIEW",
    ("GET", "/api/records"): "RECORD_VIEW",
    ("PUT", "/api/records/<int:record_id>"): "RECORD_EDIT",
    ("DELETE", "/api/records/<int:record_id>"): "RECORD_DELETE",
    ("POST", "/api/scan"): "LEGACY_SCAN",
}


def _routes() -> list[dict]:
    return extract()


def _keys(route: dict) -> tuple[str, str]:
    return (route["method"], route["path"])


def _all_param_free_api_paths() -> list[tuple[str, str]]:
    return sorted(key for key in PARAM_FREE_ROLES if key[1].startswith("/api/"))


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


def test_every_param_free_route_is_classified():
    routes = [route for route in _routes() if "<" not in route["path"]]
    actual = {_keys(route) for route in routes}
    pinned = set(PARAM_FREE_ROLES) | PARAM_FREE_PUBLIC
    assert actual == pinned, (
        f"unclassified: {sorted(actual - pinned)}\n"
        f"stale entries: {sorted(pinned - actual)}"
    )


def test_pinned_role_sets_match_the_source():
    """A route with no role guard at all is reachable by every logged-in role.

    ``extract_routes`` reports that as an empty ``roles`` list; normalise it here
    so the pin expresses intent ("everyone") rather than the absence of a guard.
    """
    routes = {_keys(route): route for route in _routes()}
    for key, expected in PARAM_FREE_ROLES.items():
        route = routes[key]
        actual = frozenset(route["roles"]) or EVERY_ROLE
        assert actual == expected, (
            f"{key[0]} {key[1]}: expected {sorted(expected)}, got {sorted(actual)}"
        )


def test_no_route_relies_solely_on_a_noop_scope_guard():
    """DEVIATION D2 is fixed: every scope-guarded route also has a real guard.

    ``require_product_model_access`` / ``require_supplier_access`` still no-op
    because ``current_operator_id()`` returns None. Any route whose *only*
    guard is a scope check is therefore reachable by every logged-in role, which
    is exactly the hole that used to exist. This test keeps it closed.
    """
    offenders = [
        _keys(route)
        for route in _routes()
        if not route["public"] and not route["roles"] and route["scopes"]
    ]
    assert offenders == [], (
        f"routes guarded only by no-op scope checks: {sorted(offenders)}. "
        "Add require_capability(...) and update docs/PERMISSION_MATRIX.md"
    )


def test_capability_guarded_routes_are_the_expected_set():
    routes = {_keys(route): route for route in _routes()}
    actual = {
        key: route["capabilities"]
        for key, route in routes.items()
        if route["capabilities"]
    }
    expected = {key: [value] for key, value in CAPABILITY_GUARDED_ROUTES.items()}
    assert actual == expected, (
        "the set of capability-guarded routes changed. Update "
        "CAPABILITY_GUARDED_ROUTES and docs/PERMISSION_MATRIX.md"
    )


def test_service_guarded_routes_are_exactly_the_known_set():
    routes = _routes()
    actual = {_keys(route) for route in routes if route["guarded_in_service"]}
    assert actual == EXPECTED_SERVICE_GUARDED_ROUTES, (
        "guards moved between handler and service layer. A permission audit that "
        "only reads handler bodies will now produce wrong answers; update "
        "EXPECTED_SERVICE_GUARDED_ROUTES and DEVIATION D5"
    )


# --------------------------------------------------------------------------
# 2. Capability table invariants
# --------------------------------------------------------------------------


def test_capability_table_covers_exactly_the_three_roles():
    assert set(ROLE_CAPABILITIES) == VALID_ROLES


def test_admin_holds_every_capability():
    """ADMIN is a superuser; express that as an invariant, not a special case."""
    all_capabilities = frozenset(item for item in Capability)
    assert capabilities_for_role(ROLE_ADMIN) == all_capabilities


def test_every_capability_is_held_by_at_least_one_role():
    orphans = [item.value for item in Capability if not roles_for(item)]
    assert orphans == [], f"capabilities granted to nobody: {orphans}"


def test_operations_cannot_reach_historical_records():
    """The frontend allowedViews() gives OPERATIONS no my-records view.

    The backend must agree — this is the DEVIATION D3 fix.
    """
    for capability in (
        Capability.RECORD_VIEW,
        Capability.RECORD_EDIT,
        Capability.RECORD_DELETE,
        Capability.RECORD_ADMIN,
    ):
        assert not capabilities_for_role(ROLE_OPERATIONS).issuperset({capability}), (
            f"OPERATIONS must not hold {capability.value}"
        )
        assert capability in capabilities_for_role(ROLE_WAREHOUSE) or capability in capabilities_for_role(
            ROLE_ADMIN
        )


def test_warehouse_cannot_reach_purchase_or_lingxing_capabilities():
    granted = capabilities_for_role(ROLE_WAREHOUSE)
    for capability in (
        Capability.PURCHASE_MANAGE,
        Capability.LINGXING_PUSH,
        Capability.INVENTORY_SYNC,
    ):
        assert capability not in granted, f"WAREHOUSE must not hold {capability.value}"


def test_operations_cannot_reach_batch_or_receipt_capabilities():
    granted = capabilities_for_role(ROLE_OPERATIONS)
    for capability in (
        Capability.BATCH_GENERATE,
        Capability.BATCH_REGISTER,
        Capability.RECEIPT_CREATE,
        Capability.PRODUCTION_ORDER_CREATE,
        Capability.FINISHED_GOODS_INBOUND,
        Capability.QUALITY_RELEASE,
    ):
        assert capability not in granted, f"OPERATIONS must not hold {capability.value}"


# --------------------------------------------------------------------------
# 3. HTTP behaviour
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
        "ADMIN": (admin, admin_csrf),
        "WAREHOUSE": (warehouse, warehouse_csrf),
        "OPERATIONS": (operations, operations_csrf),
    }


def _call(client, csrf, method: str, path: str):
    if method == "GET":
        return client.get(path)
    return client.open(path, method=method, json={}, headers={"X-CSRF-Token": csrf})


@pytest.mark.parametrize("method,path", _all_param_free_api_paths())
def test_anonymous_is_rejected_with_401(principals, method, path):
    """No anonymous access to any /api/ route outside PUBLIC_PATHS."""
    assert path not in PUBLIC_PATHS
    response = _call(principals["app"].test_client(), "", method, path)
    assert response.status_code == 401, (
        f"{method} {path} answered {response.status_code} anonymously; "
        "every /api/ route except /api/health and /api/auth/login must require login"
    )


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


@pytest.mark.parametrize("method,path", _all_param_free_api_paths())
@pytest.mark.parametrize("role", ["ADMIN", "WAREHOUSE", "OPERATIONS"])
def test_role_matrix(principals, role, method, path):
    """The single authoritative matrix: every role, every param-free route."""
    allowed = role in PARAM_FREE_ROLES[(method, path)]
    client, csrf = principals[role]
    response = _call(client, csrf, method, path)
    if allowed:
        assert response.status_code not in {401, 403}, (
            f"{role} was denied on {method} {path} ({response.status_code}) but the "
            "pinned matrix allows it"
        )
    else:
        assert response.status_code == 403, (
            f"{role} got {response.status_code} on {method} {path}; expected 403"
        )


# --------------------------------------------------------------------------
# 4. Fixed deviations, pinned as behaviour
# --------------------------------------------------------------------------


def test_records_family_is_closed_to_operations(principals):
    """DEVIATION D3, now fixed.

    ``editable_record()``'s owner check is still dead code, but the route no
    longer depends on it: ``require_capability(RECORD_EDIT)`` rejects OPERATIONS
    before the lookup runs, so the answer is 403 rather than 404.
    """
    client, csrf = principals["OPERATIONS"]
    response = client.put(
        "/api/records/999999",
        json={"remarks": "校对", "partCodes": ["NOT-A-REAL-CODE"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403, (
        f"expected 403 for OPERATIONS, got {response.status_code}"
    )

    deleted = client.delete(
        "/api/records/999999",
        json={"reason": "测试"},
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 403, (
        f"expected 403 for OPERATIONS on delete, got {deleted.status_code}"
    )


def test_records_family_still_reachable_by_warehouse(principals):
    """The guard must not over-restrict: WAREHOUSE keeps access."""
    client, csrf = principals["WAREHOUSE"]
    response = client.put(
        "/api/records/999999",
        json={"remarks": "校对", "partCodes": ["NOT-A-REAL-CODE"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404, (
        f"WAREHOUSE should reach the lookup and get 404, got {response.status_code}"
    )


def test_legacy_scan_is_closed_to_operations(principals):
    client, csrf = principals["OPERATIONS"]
    response = client.post("/api/scan", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403, (
        f"expected 403 for OPERATIONS on /api/scan, got {response.status_code}"
    )


def test_deviation_d5_service_layer_guard_is_effective(principals):
    """``/pass`` has no guard in its handler, but the service layer enforces ADMIN.

    This is the counter-example that makes a handler-only audit wrong.
    """
    client, csrf = principals["WAREHOUSE"]
    response = client.post(
        "/api/batch-trace-records/999999/pass", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403, (
        "warehouse must be denied by require_admin() inside transition_batch_quality"
    )


def test_deviation_d1_role_guards_share_one_implementation():
    """DEVIATION D1, now fixed: the two guards are literally the same function."""
    from traceability import auth

    assert auth.require_admin_or_warehouse is auth.require_warehouse


def test_scope_ids_are_still_echoed_by_the_api(principals):
    """DEVIATION D4 (API surface): user payloads still advertise scope ids."""
    client, csrf = principals["ADMIN"]
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
    """The three guards must stay distinguishable, and VALID_ROLES must not grow."""
    from traceability import auth

    assert {"ADMIN", "WAREHOUSE", "OPERATIONS"} == VALID_ROLES

    def denies(guard, role):
        original = auth.current_user
        auth.current_user = lambda: {"id": 1, "role": role, "display_name": role}
        try:
            guard()
            return False
        except auth.AuthError:
            return True
        finally:
            auth.current_user = original

    for role in VALID_ROLES:
        assert denies(auth.require_admin, role) == (role != ROLE_ADMIN)
        assert denies(auth.require_warehouse, role) == (role == ROLE_OPERATIONS)
        assert denies(auth.require_operations, role) == (role == ROLE_WAREHOUSE)


def test_require_capability_rejects_a_role_lacking_the_capability():
    from traceability import auth

    original = auth.current_user
    auth.current_user = lambda: {"id": 1, "role": ROLE_OPERATIONS, "display_name": "op"}
    try:
        with pytest.raises(auth.AuthError) as error:
            auth.require_capability(Capability.RECORD_DELETE)
        assert error.value.status == 403
        # A capability the role does hold must pass.
        auth.require_capability(Capability.PURCHASE_MANAGE)
    finally:
        auth.current_user = original


def test_require_capability_returns_401_when_not_logged_in():
    from traceability import auth

    original = auth.current_user
    auth.current_user = lambda: None
    try:
        with pytest.raises(auth.AuthError) as error:
            auth.require_capability(Capability.TRACE_VIEW)
        assert error.value.status == 401
    finally:
        auth.current_user = original
