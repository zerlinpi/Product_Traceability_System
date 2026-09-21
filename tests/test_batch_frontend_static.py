"""Static / DOM assertions for the batch-traceability frontend (task 15.2).

Feature: batch-traceability. This is a UI presentation change (frontend views),
so — following the static-assertion style of ``tests/test_sidebar_styles_static``
— no property-based tests are introduced here. Instead these structural
invariants are asserted against ``templates/index_v2.html`` and
``static/app_v2.js``.

The focus is the treadmill (走步机) field-entry interface (现场录入): it must
present **only** batch QR-code registration and must **not** expose the legacy
per-machine main-code (每台一个主码) / per-component assembly (逐部件扫码装配)
entry.

Covers:
- Requirement 7.1: the per-machine main-code + per-component assembly entry does
  not appear in the field-entry interface.
- Requirement 7.2: field registration for a treadmill product offers only batch
  QR-code registration, with no per-machine main-code / per-component scan option.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "templates" / "index_v2.html"
JS_PATH = ROOT / "static" / "app_v2.js"

# The id of the treadmill field-entry (现场批次登记) view/section.
FIELD_ENTRY_VIEW_ID = "view-batch-entry"

# Markers that MUST be present in the field-entry section: batch QR scan only.
REQUIRED_BATCH_SCAN_MARKERS = [
    'id="batch-entry-form"',   # the single batch-scan form
    'id="batch-entry-code"',   # batch QR code input
    "批次扫码登记",              # step label: batch scan registration
    "扫描批次二维码",            # instruction: scan the batch QR code
]

# Markers that MUST NOT appear inside the field-entry section. Each denotes a
# per-machine main-code / per-component assembly entry element from the retired
# treadmill flow. NOTE: the descriptive copy legitimately says "无需逐台、逐部件
# 扫码" (no need for per-unit / per-component scanning), so those exact phrases
# are deliberately excluded from this forbidden list; the markers below target
# the actual entry controls/labels rather than that negating prose.
FORBIDDEN_FIELD_ENTRY_MARKERS = [
    "装配扫码",        # assembly scanning station
    "scanner-livebar", # legacy assembly scanner live bar element
    "产品主码",        # per-machine master-code (scan target/label)
    "部件码",          # per-component code (scan target/label)
]

# Legacy per-machine/per-component assembly view markers that must not exist
# anywhere in the v2 template (the whole assembly-scanner view is retired).
FORBIDDEN_LEGACY_VIEW_MARKERS = [
    'id="view-scanner"',
    'data-view="scanner"',
    "装配扫码台",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def extract_section(html: str, view_id: str) -> str:
    """Return the full ``<section ...id="{view_id}"...> ... </section>`` block.

    Handles nested ``<section>`` elements by tracking open/close depth so the
    returned block ends at the matching closing tag, not the first inner one.
    """
    marker = f'id="{view_id}"'
    marker_pos = html.index(marker)
    open_pos = html.rindex("<section", 0, marker_pos)

    depth = 0
    for match in re.finditer(r"<section\b|</section>", html[open_pos:]):
        if match.group().startswith("</"):
            depth -= 1
            if depth == 0:
                end = open_pos + match.end()
                return html[open_pos:end]
        else:
            depth += 1
    raise AssertionError(f"unbalanced <section> for {view_id}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def html_text() -> str:
    assert HTML_PATH.exists(), f"missing template: {HTML_PATH}"
    return HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def field_entry_section(html_text: str) -> str:
    return extract_section(html_text, FIELD_ENTRY_VIEW_ID)


@pytest.fixture(scope="module")
def js_text() -> str:
    assert JS_PATH.exists(), f"missing script: {JS_PATH}"
    return JS_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Requirement 7.2: field-entry offers only batch QR registration
# --------------------------------------------------------------------------- #
def test_field_entry_view_present(html_text):
    """The treadmill field-entry view exists in the served template."""
    assert f'id="{FIELD_ENTRY_VIEW_ID}"' in html_text, (
        f"field-entry view {FIELD_ENTRY_VIEW_ID} not found in index_v2.html"
    )


@pytest.mark.parametrize("marker", REQUIRED_BATCH_SCAN_MARKERS)
def test_field_entry_contains_batch_scan(field_entry_section, marker):
    """Requirement 7.2: the field-entry view provides batch QR registration."""
    assert marker in field_entry_section, (
        f"field-entry view must contain batch-scan marker {marker!r}"
    )


# --------------------------------------------------------------------------- #
# Requirement 7.1: no per-machine / per-component entry in field-entry view
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", FORBIDDEN_FIELD_ENTRY_MARKERS)
def test_field_entry_has_no_per_machine_or_component_entry(field_entry_section, marker):
    """Requirements 7.1, 7.2.

    The field-entry view must not expose per-machine main-code or per-component
    assembly entry controls.
    """
    assert marker not in field_entry_section, (
        f"field-entry view must NOT contain per-machine/per-component entry "
        f"marker {marker!r}"
    )


def test_field_entry_has_single_scan_input(field_entry_section):
    """Requirement 7.2.

    Only one code-scan input (the batch QR input) exists in the field-entry
    view — there is no second input for scanning a machine SN or component code.
    """
    scan_inputs = re.findall(r'<input\b[^>]*name="code"', field_entry_section)
    assert len(scan_inputs) == 1, (
        f"field-entry view must expose exactly one code-scan input "
        f"(the batch QR input), found {len(scan_inputs)}"
    )


# --------------------------------------------------------------------------- #
# Requirement 7.1: the legacy assembly-scanner view is fully removed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", FORBIDDEN_LEGACY_VIEW_MARKERS)
def test_no_legacy_assembly_scanner_view(html_text, marker):
    """Requirement 7.1: retired per-machine/per-component assembly view is gone."""
    assert marker not in html_text, (
        f"v2 template must NOT contain retired assembly-scanner marker {marker!r}"
    )


# --------------------------------------------------------------------------- #
# Requirement 7.2: field-entry submits only the batch-scan endpoint
# --------------------------------------------------------------------------- #
def test_field_entry_posts_batch_scan_endpoint(js_text):
    """Requirement 7.2.

    The field-entry submit handler posts the batch-scan endpoint, confirming the
    only registration path from the field-entry view is batch QR registration.
    """
    assert '"/api/batch-entry/scan"' in js_text, (
        "app_v2.js must wire the field-entry form to /api/batch-entry/scan"
    )
