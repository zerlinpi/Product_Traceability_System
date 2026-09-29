"""Static assertions for the batch-traceability field-entry UI (task 15.2).

Feature: batch-traceability. A UI presentation concern, so — like
``tests/test_sidebar_styles_static`` — no property-based tests: structural
invariants are asserted against the frontend sources.

Since the UI rebuild on Fantastic-admin the field-entry page (现场批次登记) is
``frontend/apps/web/src/views/batch-entry/index.vue`` (route ``batch-entry``),
and its backend calls go through ``src/api/modules/batches.ts``. The intent is
unchanged: the treadmill (走步机) field entry presents **only** batch QR-code
registration and never the retired per-machine main-code (每台一个主码) /
per-component assembly (逐部件扫码装配) entry.

Covers:
- Requirement 7.1: the per-machine main-code + per-component assembly entry does
  not appear in the field-entry interface.
- Requirement 7.2: field registration for a treadmill product offers only batch
  QR-code registration, with no per-machine main-code / per-component scan option.
"""
from __future__ import annotations

import re

import pytest

from frontend_sources import (
    SRC,
    VIEWS,
    all_source_text,
    api_module,
    menu_pages,
    opening_tags,
    pages,
    read,
    template_of,
)

# The route / page key of the treadmill field-entry (现场批次登记) view.
FIELD_ENTRY_PAGE = "batch-entry"
FIELD_ENTRY_VIEW = VIEWS / "batch-entry" / "index.vue"

# Markers that MUST be present in the field-entry template: batch QR scan only.
REQUIRED_BATCH_SCAN_MARKERS = [
    'id="batch-entry-form"',   # the single batch-scan form
    'id="batch-entry-code"',   # batch QR code input
    "批次扫码登记",              # step label: batch scan registration
    "扫描批次二维码",            # instruction: scan the batch QR code
]

# Markers that MUST NOT appear in the field-entry template. Each denotes a
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

# The retired per-machine/per-component assembly view must not exist anywhere
# in the frontend: no page, no route, no copy.
FORBIDDEN_LEGACY_VIEW_MARKERS = [
    "'scanner'",       # a route / page key named scanner
    "views/scanner",   # a scanner view component
    "装配扫码台",       # the assembly station title
]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def field_entry_template() -> str:
    assert FIELD_ENTRY_VIEW.exists(), f"missing view: {FIELD_ENTRY_VIEW}"
    return template_of(read(FIELD_ENTRY_VIEW))


@pytest.fixture(scope="module")
def frontend_text() -> str:
    return all_source_text(".vue", ".ts")


# --------------------------------------------------------------------------- #
# Requirement 7.2: field-entry offers only batch QR registration
# --------------------------------------------------------------------------- #
def test_field_entry_view_present():
    """The treadmill field-entry page is routed and reachable by warehouse staff."""
    page = pages().get(FIELD_ENTRY_PAGE)
    assert page, f"route table has no {FIELD_ENTRY_PAGE!r} page"
    assert "batch-entry/index.vue" in page["views"]
    assert set(page["roles"]) == {"ADMIN", "WAREHOUSE"}
    assert FIELD_ENTRY_PAGE in menu_pages()["WAREHOUSE"]


@pytest.mark.parametrize("marker", REQUIRED_BATCH_SCAN_MARKERS)
def test_field_entry_contains_batch_scan(field_entry_template, marker):
    """Requirement 7.2: the field-entry view provides batch QR registration."""
    assert marker in field_entry_template, (
        f"field-entry view must contain batch-scan marker {marker!r}"
    )


# --------------------------------------------------------------------------- #
# Requirement 7.1: no per-machine / per-component entry in field-entry view
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", FORBIDDEN_FIELD_ENTRY_MARKERS)
def test_field_entry_has_no_per_machine_or_component_entry(field_entry_template, marker):
    """Requirements 7.1, 7.2.

    The field-entry view must not expose per-machine main-code or per-component
    assembly entry controls.
    """
    assert marker not in field_entry_template, (
        f"field-entry view must NOT contain per-machine/per-component entry "
        f"marker {marker!r}"
    )


def test_field_entry_has_single_scan_input(field_entry_template):
    """Requirement 7.2.

    Only one code-scan input (the batch QR input) exists in the field-entry
    view — there is no second input for scanning a machine SN or component code.
    """
    inputs = opening_tags(field_entry_template, "ElInput") + opening_tags(field_entry_template, "input")
    scan_inputs = [tag for tag in inputs if re.search(r'\sname="code"', tag)]
    assert len(scan_inputs) == 1, (
        f"field-entry view must expose exactly one code-scan input "
        f"(the batch QR input), found {len(scan_inputs)}"
    )


# --------------------------------------------------------------------------- #
# Requirement 7.1: the legacy assembly-scanner view is fully removed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", FORBIDDEN_LEGACY_VIEW_MARKERS)
def test_no_legacy_assembly_scanner_view(frontend_text, marker):
    """Requirement 7.1: retired per-machine/per-component assembly view is gone."""
    assert marker not in frontend_text, (
        f"frontend must NOT contain retired assembly-scanner marker {marker!r}"
    )
    assert not (SRC / "views" / "scanner").exists()


# --------------------------------------------------------------------------- #
# Requirement 7.2: field-entry submits only the batch-scan endpoint
# --------------------------------------------------------------------------- #
def test_field_entry_posts_batch_scan_endpoint():
    """Requirement 7.2.

    The field-entry submit handler posts the batch-scan endpoint (idempotently),
    confirming the only registration path from the field-entry view is batch QR
    registration.
    """
    module = api_module("batches")
    assert re.search(
        r"api\.post<[^>]*>\('/api/batch-entry/scan'.*idempotent: 'batch-entry\.scan'", module
    ), "batches.ts must post /api/batch-entry/scan with the batch-entry.scan idempotency scope"
    view = read(FIELD_ENTRY_VIEW)
    assert "batchesApi.batchEntryScan(" in view
    # The retired per-unit scan endpoints are not wired anywhere in the UI.
    modules = all_source_text(".ts", base=SRC / "api")
    assert not re.search(r"'/api/scan(?:/[a-z]+)?'", modules), (
        "the frontend must not call the retired per-unit /api/scan endpoints"
    )
