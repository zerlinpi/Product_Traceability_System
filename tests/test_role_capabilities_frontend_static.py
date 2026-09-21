from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index_v2.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app_v2.js").read_text(encoding="utf-8")


def test_admin_sees_settings_and_user_management_only_in_admin_navigation():
    assert 'data-view="users" data-role="ADMIN"' in HTML
    assert 'data-view="settings"' not in HTML  # settings is the dedicated footer action
    assert 'data-go="settings" data-role="ADMIN"' in HTML
    assert "用户管理" in HTML
    assert "录入员管理" not in HTML
    assert 'id="view-settings" data-role="ADMIN"' in HTML


def test_warehouse_capabilities_include_orders_scan_and_focus_recovery():
    for view in ("production-orders", "scan-gun", "inbound-receipts"):
        assert f'data-view="{view}" data-role="WAREHOUSE"' in HTML
    assert 'id="scan-gun-code"' in HTML
    assert "focusScanGun" in JS
    assert "inboundForm.elements.quantity.focus()" in JS
    assert "loadScanGun();" in JS


def test_operations_capabilities_include_export_sync_and_factory_progress():
    assert 'data-view="purchase-orders" data-role="OPERATIONS"' in HTML
    assert 'data-view="inventory-sync" data-role="OPERATIONS"' in HTML
    assert "/export" in JS
    assert "/factory-progress" in JS
    assert 'id="inventory-sync-button"' in HTML


def test_final_role_model_is_used_by_navigation_without_operator_role():
    assert 'data-role="OPERATOR"' not in HTML
    assert 'role === "OPERATOR"' not in JS
    assert 'role === "WAREHOUSE"' in JS
    assert 'role === "OPERATIONS"' in JS
