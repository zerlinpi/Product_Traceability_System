"""Static checks for the sidebar style refactor (spec: sidebar-style-refresh).

This feature is a CSS/style refactor (UI presentation). Per the design's PBT
applicability assessment, no property-based tests are introduced; instead these
static structural invariants are asserted against ``static/styles_v2.css`` and
``templates/index_v2.html``.

Covers the static-check portion of Correctness Properties 1-19:
- Property 1  : each top-level sidebar selector defined exactly once (non-media).
- Property 2  : ``.sidebar-overlay { display: none }`` appears exactly once.
- Property 3  : no duplicate ``max-width: 760px`` / ``max-width: 860px`` blocks.
- Property 4  : sidebar rules (outside ``:root``) carry no hex color literals.
- Property 11 : ``.nav-button.active`` / ``.rail-button.active`` use --primary
                and --primary-soft tokens.
- Property 19 : stylesheet ``?v=`` version strictly increased and consistent.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 5.2, 8.1, 8.3, 11.1, 11.2**
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "static" / "styles_v2.css"
HTML_PATH = ROOT / "templates" / "index_v2.html"

# The pre-refactor stylesheet version documented in design.md (``?v=20260719.4``).
# Property 19 requires the current version to be strictly greater than this.
BASELINE_VERSION = "20260719.4"

# The ten top-level sidebar selectors that must each appear exactly once as an
# independent (bare) rule selector in non-media scope (Requirement 1.1).
TOP_LEVEL_SELECTORS = [
    ".sidebar",
    ".sidebar-rail",
    ".sidebar-brand",
    ".rail-nav",
    ".rail-button",
    ".nav-button",
    ".nav-group-label",
    ".sidebar-panel",
    ".sidebar-user",
    ".sidebar-footer-nav",
]

# First-compound-selector prefixes that mark a rule as "sidebar scoped" for the
# hex-literal check. Non-sidebar selectors in the file may legitimately contain
# hex literals and are out of scope for this refactor.
SIDEBAR_SCOPE_PREFIXES = (
    ".sidebar",  # matches .sidebar, .sidebar-rail, .sidebar-panel, .sidebar-user, ...
    ".rail-nav",
    ".rail-button",
    ".rail-footer",
    ".rail-spacer",
    ".nav-button",
    ".nav-group",  # matches .nav-group and .nav-group-label
    ".nav-icon",
    ".main-nav",
)

HEX_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b")


# --------------------------------------------------------------------------- #
# CSS parsing helpers
# --------------------------------------------------------------------------- #
def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_rules(css: str) -> list[dict]:
    """Parse CSS into a flat list of style rules.

    Each returned rule is a dict with:
      - ``selector``: the raw selector list text (may be comma separated)
      - ``body``: the declaration block text (without braces)
      - ``media``: list of enclosing at-rule preludes (empty => top-level scope)

    Nested ``@media`` rules are handled by tracking an at-rule stack, so rules
    inside a media query record that context and are excluded from top-level
    (non-media) scope checks.
    """
    css = _strip_comments(css)
    rules: list[dict] = []
    at_rule_stack: list[str] = []
    prelude: list[str] = []
    i, n = 0, len(css)
    while i < n:
        c = css[i]
        if c == "{":
            selector = "".join(prelude).strip()
            prelude = []
            if selector.startswith("@"):
                # at-rule block (e.g. @media): enter and keep parsing children.
                at_rule_stack.append(selector)
                i += 1
                continue
            # Normal rule: declaration blocks contain no nested braces.
            close = css.index("}", i)
            body = css[i + 1 : close]
            rules.append(
                {
                    "selector": selector,
                    "body": body,
                    "media": list(at_rule_stack),
                }
            )
            i = close + 1
            continue
        if c == "}":
            if at_rule_stack:
                at_rule_stack.pop()
            prelude = []
            i += 1
            continue
        prelude.append(c)
        i += 1
    return rules


def selector_parts(selector: str) -> list[str]:
    """Split a comma separated selector list into trimmed parts."""
    return [p.strip() for p in selector.split(",") if p.strip()]


def first_compound(part: str) -> str:
    """Return the first compound selector (before any combinator/space)."""
    return re.split(r"[\s>+~]", part.strip(), maxsplit=1)[0]


def is_sidebar_scoped(selector: str) -> bool:
    for part in selector_parts(selector):
        head = first_compound(part)
        if head == ":root":
            continue
        if any(head.startswith(prefix) for prefix in SIDEBAR_SCOPE_PREFIXES):
            return True
    return False


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def css_text() -> str:
    assert CSS_PATH.exists(), f"missing stylesheet: {CSS_PATH}"
    return CSS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules(css_text: str) -> list[dict]:
    return parse_rules(css_text)


@pytest.fixture(scope="module")
def html_text() -> str:
    assert HTML_PATH.exists(), f"missing template: {HTML_PATH}"
    return HTML_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Property 1: each top-level sidebar selector defined exactly once (non-media)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target", TOP_LEVEL_SELECTORS)
def test_top_level_selector_defined_exactly_once(rules, target):
    """Requirement 1.1 / Property 1.

    The bare selector must appear exactly once as an independent rule selector
    in non-media scope. Compound/descendant selectors such as
    ``.rail-button.active`` or ``.sidebar-brand .brand-symbol`` do NOT count.
    """
    count = 0
    for rule in rules:
        if rule["media"]:
            continue  # only non-media (top-level) scope
        for part in selector_parts(rule["selector"]):
            if part == target:
                count += 1
    assert count == 1, (
        f"{target} must appear exactly once as an independent non-media rule "
        f"selector, found {count}"
    )


# --------------------------------------------------------------------------- #
# Property 2: `.sidebar-overlay { display: none }` appears exactly once
# --------------------------------------------------------------------------- #
def test_sidebar_overlay_display_none_once(rules):
    """Requirement 1.2 / Property 2."""
    display_none = re.compile(r"display\s*:\s*none\b")
    count = 0
    for rule in rules:
        if ".sidebar-overlay" in selector_parts(rule["selector"]):
            if display_none.search(rule["body"]):
                count += 1
    assert count == 1, (
        f".sidebar-overlay must declare `display: none` exactly once across the "
        f"whole stylesheet, found {count}"
    )


# --------------------------------------------------------------------------- #
# Property 3: no duplicate equivalent media-query breakpoints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("breakpoint_px", [760, 860])
def test_no_duplicate_media_breakpoint(css_text, breakpoint_px):
    """Requirement 1.3 / Property 3.

    Each of the 760px / 860px breakpoints maps to at most one media query block.
    """
    css = _strip_comments(css_text)
    pattern = re.compile(
        r"@media[^{}]*max-width\s*:\s*%dpx" % breakpoint_px
    )
    matches = pattern.findall(css)
    assert len(matches) <= 1, (
        f"max-width: {breakpoint_px}px must map to at most one media query "
        f"block, found {len(matches)}"
    )


# --------------------------------------------------------------------------- #
# Property 11: active states reference brand tokens
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_selector", [".nav-button.active", ".rail-button.active"])
def test_active_state_uses_brand_tokens(rules, active_selector):
    """Requirements 5.2, 8.1 / Property 11."""
    matching = [
        rule
        for rule in rules
        if active_selector in selector_parts(rule["selector"])
    ]
    assert matching, f"missing rule for {active_selector}"
    body = " ".join(rule["body"] for rule in matching)
    assert "var(--primary)" in body, (
        f"{active_selector} must reference var(--primary)"
    )
    assert "var(--primary-soft)" in body, (
        f"{active_selector} must reference var(--primary-soft)"
    )


# --------------------------------------------------------------------------- #
# Property 4: no hex color literals in sidebar-scoped rules (except :root)
# --------------------------------------------------------------------------- #
def test_no_hex_literals_in_sidebar_rules(rules):
    """Requirements 1.4, 8.3 / Property 4."""
    offenders = []
    for rule in rules:
        selector = rule["selector"]
        if ":root" in selector:
            continue
        if not is_sidebar_scoped(selector):
            continue
        hits = HEX_LITERAL.findall(rule["body"])
        if hits:
            offenders.append((selector, hits))
    assert not offenders, (
        "sidebar-scoped rules must reference :root tokens, not hex literals; "
        f"offending rules: {offenders}"
    )


# --------------------------------------------------------------------------- #
# Property 19: stylesheet cache version strictly increased and consistent
# --------------------------------------------------------------------------- #
def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(chunk) for chunk in version.split("."))


def test_stylesheet_version_bumped_and_consistent(html_text):
    """Requirements 11.1, 11.2 / Property 19."""
    versions = re.findall(r"static/styles_v2\.css\?v=([0-9.]+)", html_text)
    assert versions, "no versioned reference to static/styles_v2.css found"

    unique = set(versions)
    assert len(unique) == 1, (
        f"all styles_v2.css references must use the same version, found {unique}"
    )

    current = versions[0]
    assert _version_tuple(current) > _version_tuple(BASELINE_VERSION), (
        f"stylesheet version {current} must be strictly greater than the "
        f"pre-refactor baseline {BASELINE_VERSION}"
    )
