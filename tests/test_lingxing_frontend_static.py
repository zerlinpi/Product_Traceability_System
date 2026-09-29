"""Lingxing (领星) screens in the frontend: who sees them, and no secrets.

The pages live in ``frontend/apps/web/src/views`` and their role policy in
``src/router/routes.ts`` (see ``tests/frontend_sources.py``).
"""

from frontend_sources import (
    DIST,
    all_source_text,
    api_module,
    menu_pages,
    page_source,
    pages,
    read,
    source_files,
)


def test_lingxing_views_follow_operations_and_warehouse_roles():
    table = pages()
    assert set(table["purchase-orders"]["roles"]) == {"ADMIN", "OPERATIONS"}
    assert set(table["inbound-receipts"]["roles"]) == {"ADMIN", "WAREHOUSE"}
    assert "purchase-orders" in menu_pages()["OPERATIONS"]
    assert "inbound-receipts" in menu_pages()["WAREHOUSE"]
    purchase_orders = page_source("purchase-orders")
    assert "推送领星" in purchase_orders
    assert "推送入库" in purchase_orders
    assert "syncStatus" in purchase_orders
    operations = api_module("operations")
    assert "/push`" in operations and "/sync-status`" in operations


def test_frontend_never_embeds_live_credentials_or_tokens():
    # Sources and the shipped bundle alike.
    source = all_source_text(".vue", ".ts")
    bundle = "\n".join(read(path) for path in source_files(".js", ".html", ".css", base=DIST))
    for text in (source, bundle):
        assert "PTS_LINGXING_APP_SECRET" not in text
        assert "access_token=" not in text
        assert "refresh_token=" not in text
    settings = page_source("settings")
    assert 'type="password"' in settings
    assert "敏感值仅脱敏回显" in settings
    assert 'id="settings-lingxing-masked"' in settings
    # Secrets are write-only: the inputs start blank after every load.
    assert "form.appSecret = ''" in settings
