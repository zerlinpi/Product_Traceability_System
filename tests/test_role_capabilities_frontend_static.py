"""The three-role navigation of the frontend (ADMIN / WAREHOUSE / OPERATIONS).

Role → page policy lives in ``frontend/apps/web/src/router/routes.ts``:
``PAGES`` says which roles may open a page (``meta.auth``), ``MENU_GROUPS``
what each role's navigation shows. The backend capability matrix remains the
real enforcement (see ``tests/test_frontend_build.py`` for the cross-check).
"""

import re

from frontend_sources import (
    SRC,
    all_source_text,
    api_module,
    menu_pages,
    page_source,
    pages,
    read,
)


def test_admin_sees_settings_and_user_management_only_in_admin_navigation():
    table = pages()
    assert table["users"]["roles"] == ["ADMIN"]
    assert table["settings"]["roles"] == ["ADMIN"]
    assert table["users"]["title"] == "用户管理"
    menus = menu_pages()
    assert "users" in menus["ADMIN"] and "settings" in menus["ADMIN"]
    for role in ("WAREHOUSE", "OPERATIONS"):
        assert "users" not in menus[role]
        assert "settings" not in menus[role]
    assert "录入员管理" not in all_source_text(".vue", ".ts")


def test_warehouse_capabilities_include_orders_scan_and_focus_recovery():
    for view in ("production-orders", "scan-gun", "inbound-receipts"):
        assert view in menu_pages()["WAREHOUSE"]
    scan_gun = page_source("scan-gun")
    assert 'id="scan-gun-code"' in scan_gun
    assert "function focusScanGun()" in scan_gun
    # A successful lookup moves focus to the quantity box …
    lookup = scan_gun[scan_gun.index("async function submitScanGunLookup"):]
    lookup = lookup[: lookup.index("\n}\n")]
    assert "focusQuantity()" in lookup
    # … and a confirmed stock-in resets the page back to the QR box.
    inbound = scan_gun[scan_gun.index("async function submitScanGunInbound"):]
    inbound = inbound[: inbound.index("\n}\n")]
    assert "loadScanGun()" in inbound
    assert "focusScanGun()" in scan_gun[scan_gun.index("function loadScanGun()"):]


def test_operations_capabilities_include_export_sync_and_factory_progress():
    assert "purchase-orders" in menu_pages()["OPERATIONS"]
    assert "inventory-sync" in menu_pages()["OPERATIONS"]
    operations = api_module("operations")
    assert "/export" in operations
    assert "/factory-progress" in operations
    assert 'id="inventory-sync-button"' in page_source("inventory-sync")


def test_final_role_model_is_used_by_navigation_without_operator_role():
    sources = all_source_text(".vue", ".ts")
    assert "'OPERATOR'" not in sources
    assert '"OPERATOR"' not in sources
    types = read(SRC / "api" / "types.ts")
    assert "export type Role = 'ADMIN' | 'WAREHOUSE' | 'OPERATIONS'" in types
    account = read(SRC / "store" / "modules" / "app" / "account.ts")
    assert "role.value === 'WAREHOUSE'" in account
    assert "role.value === 'OPERATIONS'" in account
    assert set(menu_pages()) == {"ADMIN", "WAREHOUSE", "OPERATIONS"}
    # Every role maps to exactly the roles the backend knows about.
    for page in pages().values():
        assert set(page["roles"]) <= {"ADMIN", "WAREHOUSE", "OPERATIONS"}
    assert not re.search(r"roles: \[\]", read(SRC / "router" / "routes.ts"))
