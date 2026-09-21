from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index_v2.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app_v2.js").read_text(encoding="utf-8")


def test_lingxing_views_follow_operations_and_warehouse_roles():
    assert 'id="view-purchase-orders" data-role="ADMIN,OPERATIONS"' in HTML
    assert 'id="view-inbound-receipts" data-role="ADMIN,WAREHOUSE"' in HTML
    assert 'data-view="purchase-orders" data-role="OPERATIONS"' in HTML
    assert 'data-view="inbound-receipts" data-role="WAREHOUSE"' in HTML
    assert "推送领星" in JS
    assert "推送入库" in JS
    assert "/sync-status" in JS or "syncStatus" in JS


def test_frontend_never_embeds_live_credentials_or_tokens():
    source = HTML + JS
    assert "PTS_LINGXING_APP_SECRET" not in source
    assert "access_token=" not in source
    assert "refresh_token=" not in source
    assert 'type="password"' in HTML
    assert "敏感值仅脱敏回显" in HTML
    assert "settings-lingxing-masked" in HTML

