"""Static checks for the navigation (sidebar) presentation.

The sidebar-style-refresh spec was written for the retired vanilla UI
(``static/styles_v2.css``). Since the rebuild on Fantastic-admin the sidebar is
the framework layout (``frontend/apps/web/src/layouts``) themed by
``frontend/packages/themes``. The same intents are checked against it:

- Property 1-3 (one authoritative rule set, one responsive breakpoint): the
  navigation comes from the route table only, and the mobile drawer switches
  at a single breakpoint.
- Property 4 (tokens, not hex literals): navigation components use theme
  tokens only.
- Property 11 (active state uses the brand colour): the active menu item is
  painted with ``--primary``, which is the brand green.
- Property 19 (cache busting): the entry page references content-hashed
  bundles that exist, so a deployment can never serve stale styles.

No property-based tests: this is UI presentation (see the spec's PBT note).
"""
from __future__ import annotations

import re

import pytest

from frontend_sources import (
    DIST,
    DIST_INDEX,
    FRONTEND,
    SRC,
    read,
    source_files,
    style_blocks,
)

THEMES = FRONTEND / "packages" / "themes" / "index.ts"
LAYOUTS = SRC / "layouts"
NAVIGATION_COMPONENTS = [
    LAYOUTS / "components" / "MainSidebar" / "index.vue",
    LAYOUTS / "components" / "SubSidebar" / "index.vue",
    LAYOUTS / "components" / "Menu" / "index.vue",
    LAYOUTS / "components" / "Menu" / "item.vue",
    LAYOUTS / "components" / "Menu" / "sub.vue",
    LAYOUTS / "components" / "Logo" / "index.vue",
]

#: The brand green (≈ #16803c) as the OKLCH triple the theme tokens use.
BRAND_PRIMARY = "0.53 0.14 149"
HEX_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _theme_tokens(name: str) -> dict[str, str]:
    source = read(THEMES)
    start = source.index(f"export const {name} = {{")
    block = source[start : source.index("} as const", start)]
    return dict(re.findall(r"'(--[\w-]+)': '([^']*)'", block))


@pytest.fixture(scope="module")
def light_theme() -> dict[str, str]:
    return _theme_tokens("lightTheme")


# --------------------------------------------------------------------------- #
# Property 11: the active navigation item uses the brand colour
# --------------------------------------------------------------------------- #
def test_brand_primary_is_the_green_token(light_theme):
    assert light_theme["--primary"] == BRAND_PRIMARY
    assert light_theme["--ring"] == BRAND_PRIMARY
    dark = _theme_tokens("darkTheme")
    # Dark mode keeps a (lighter) green, never the framework's neutral default.
    lightness, chroma, hue = (float(part) for part in dark["--primary"].split())
    assert chroma >= 0.1 and 140 <= hue <= 160 and lightness > 0.6


@pytest.mark.parametrize("sidebar", ["main-sidebar", "sub-sidebar"])
def test_active_state_uses_brand_tokens(light_theme, sidebar):
    assert light_theme[f"--g-{sidebar}-menu-active-bg"] == "oklch(var(--primary))"
    assert light_theme[f"--g-{sidebar}-menu-active-color"] == "oklch(var(--primary-foreground))"


# --------------------------------------------------------------------------- #
# Property 4: navigation components reference tokens, not hex literals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("component", NAVIGATION_COMPONENTS, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_no_hex_literals_in_sidebar_rules(component):
    source = read(component)
    offenders = HEX_LITERAL.findall(style_blocks(source))
    assert not offenders, f"{component.name} must use theme tokens, found {offenders}"


# --------------------------------------------------------------------------- #
# Property 1: one authoritative source for the navigation
# --------------------------------------------------------------------------- #
def test_navigation_is_generated_from_the_route_table():
    sub_sidebar = read(LAYOUTS / "components" / "SubSidebar" / "index.vue")
    main_sidebar = read(LAYOUTS / "components" / "MainSidebar" / "index.vue")
    assert "appMenuStore.allMenus" in sub_sidebar
    assert "appMenuStore.allMenus" in main_sidebar
    guards = read(SRC / "router" / "guards.ts")
    assert "asyncRoutesFor(appAccountStore.role)" in guards
    # Menu labels are never hard-coded in the layout.
    for label in ("数据概览", "批次生成", "采购订单"):
        assert label not in sub_sidebar and label not in main_sidebar


def test_sidebar_brand_shows_short_name_with_full_title():
    logo = read(LAYOUTS / "components" / "Logo" / "index.vue")
    assert "const shortTitle = '聚星同创'" in logo
    assert ':title="title"' in logo
    assert "import.meta.env.VITE_APP_TITLE" in logo


def test_sidebar_user_shows_name_and_role():
    account = read(SRC / "components" / "AppAccountButton" / "index.vue")
    assert "appAccountStore.account" in account
    assert "appAccountStore.roleLabel" in account
    assert "修改密码" in account and "退出登录" in account


# --------------------------------------------------------------------------- #
# Properties 2-3: one breakpoint and one drawer mask for narrow screens
# --------------------------------------------------------------------------- #
def test_no_duplicate_media_breakpoint():
    settings_store = read(SRC / "store" / "modules" / "app" / "settings.ts")
    assert settings_store.count("width < 1024") == 1
    layout_styles = style_blocks(read(LAYOUTS / "index.vue"))
    assert layout_styles.count('[data-mode="mobile"] {') == 1


def test_sidebar_overlay_closes_the_drawer_once():
    layout = read(LAYOUTS / "index.vue")
    masks = re.findall(r'<div[^>]*bg-black/50[^>]*@click="appSettingsStore\.toggleSidebarCollapse\(\)"', layout)
    assert len(masks) == 1


# --------------------------------------------------------------------------- #
# Property 19: the entry page always points at the current, hashed bundles
# --------------------------------------------------------------------------- #
def test_stylesheet_version_bumped_and_consistent():
    html = read(DIST_INDEX)
    references = re.findall(r'(?:href|src)="(/static/dist/assets/[^"]+)"', html)
    stylesheets = [ref for ref in references if ref.endswith(".css")]
    scripts = [ref for ref in references if ref.endswith(".js")]
    assert stylesheets and scripts, "entry page must link its CSS and JS bundles"
    for reference in references:
        assert re.search(r"-[A-Za-z0-9_-]{8}\.(?:css|js)$", reference), f"{reference} is not content-hashed"
        assert (DIST / reference.removeprefix("/static/dist/")).is_file(), f"{reference} is missing from static/dist"
    assert "?v=" not in html, "hand-maintained ?v= versions are replaced by content hashes"


def test_every_built_stylesheet_is_referenced_or_lazy_loaded():
    html = read(DIST_INDEX)
    bundles = "\n".join(read(path) for path in source_files(".js", base=DIST / "assets"))
    for stylesheet in source_files(".css", base=DIST / "assets"):
        name = stylesheet.name
        assert name in html or name in bundles, f"{name} is built but never loaded"
