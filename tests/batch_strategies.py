"""Shared Hypothesis strategies and builder helpers for batch-traceability tests.

This module centralises the property-based-testing generators used across the
batch-traceability test suite so individual property tests stay focused on a
single property. It is intentionally free of application imports at module load
time apart from :data:`ENTITY_CODE_PATTERN`, so it can be imported by pure-unit
tests as well as by tests that drive the Flask ``test_client``.

The generators cover the input spaces called out by the design's Testing
Strategy:

* planned batch quantities (台数) — valid ``[1, 999999]`` incl. boundaries, and
  out-of-range / non-integer values;
* code prefixes (前缀) — driven by ``ENTITY_CODE_PATTERN``;
* quality-hold reasons (原因) — valid ``[1, 500]`` chars incl. boundaries, and
  out-of-range values;
* ordered / unordered ISO timestamp pairs (时间范围) for range queries;
* per-unit usage (每套用量) plus client-based builder helpers that materialise a
  random product / supplier inventory batch scenario against the running app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from hypothesis import strategies as st

from traceability.codes import ENTITY_CODE_PATTERN


# ---------------------------------------------------------------------------
# Domain bounds (kept in sync with the acceptance criteria / DB CHECKs)
# ---------------------------------------------------------------------------

QUANTITY_MIN = 1
QUANTITY_MAX = 999_999

REASON_MIN = 1
REASON_MAX = 500

# ``MAX_REQUIRED_PARTS`` in app.py caps the *expanded* number of component slots
# per product (sum of per-component quantities). Builder scenarios stay well
# under this so generated products are always accepted by ``POST /api/products``.
MAX_EXPANDED_PARTS = 20


# ---------------------------------------------------------------------------
# 台数 (planned quantity) strategies
# ---------------------------------------------------------------------------


def valid_quantities() -> st.SearchStrategy[int]:
    """Integers strictly inside the accepted planned-quantity range."""

    return st.integers(min_value=QUANTITY_MIN, max_value=QUANTITY_MAX)


def boundary_quantities() -> st.SearchStrategy[int]:
    """The inclusive boundaries of the accepted range: 1 and 999999."""

    return st.sampled_from([QUANTITY_MIN, QUANTITY_MAX])


def quantities_including_boundaries() -> st.SearchStrategy[int]:
    """Valid quantities biased to also exercise the 1 / 999999 boundaries."""

    return st.one_of(boundary_quantities(), valid_quantities())


def invalid_quantities() -> st.SearchStrategy[int]:
    """Integers that fall outside the accepted range (below 1 or above max)."""

    return st.one_of(
        st.integers(max_value=QUANTITY_MIN - 1),
        st.integers(min_value=QUANTITY_MAX + 1),
    )


def non_integer_quantities() -> st.SearchStrategy[Any]:
    """Non-integer values a client might submit as a quantity."""

    return st.one_of(
        st.none(),
        st.just(""),
        st.text(min_size=1, max_size=8),
        st.floats(allow_nan=False, allow_infinity=False).filter(
            lambda value: value != int(value)
        ),
        st.booleans(),
    )


def any_quantities() -> st.SearchStrategy[int]:
    """Wide integer strategy spanning in-range, boundaries and out-of-range.

    Mirrors the task's ``st.integers()`` requirement to cover 越界 (out of
    bounds) together with the 1 / 999999 boundaries.
    """

    return st.one_of(
        boundary_quantities(),
        st.integers(min_value=QUANTITY_MIN - 100, max_value=QUANTITY_MAX + 100),
    )


# ---------------------------------------------------------------------------
# 前缀 (prefix) strategies
# ---------------------------------------------------------------------------


def entity_code_prefixes() -> st.SearchStrategy[str]:
    """Prefixes matching ``ENTITY_CODE_PATTERN`` (normalize_entity_code input)."""

    return st.from_regex(ENTITY_CODE_PATTERN, fullmatch=True)


def invalid_prefixes() -> st.SearchStrategy[Any]:
    """Values rejected by ``normalize_entity_code`` (empty / bad chars / long)."""

    return st.one_of(
        st.none(),
        st.just(""),
        st.just(" "),
        st.text(alphabet="!@# /\\", min_size=1, max_size=5),
        # too long: pattern allows at most 32 chars total
        st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=33, max_size=48),
    )


# ---------------------------------------------------------------------------
# 原因 (quality-hold reason) strategies
# ---------------------------------------------------------------------------


def valid_reasons() -> st.SearchStrategy[str]:
    """Reasons whose length is within the accepted ``[1, 500]`` range.

    A trailing non-whitespace character guarantees the value is not
    whitespace-only after trimming.
    """

    return st.text(min_size=REASON_MIN - 1, max_size=REASON_MAX - 1).map(
        lambda body: (body + "有")[:REASON_MAX]
    )


def boundary_reasons() -> st.SearchStrategy[str]:
    """Reasons at the inclusive length boundaries: 1 and 500 characters."""

    return st.sampled_from(["原", "原" * REASON_MAX])


def reasons_including_boundaries() -> st.SearchStrategy[str]:
    """Valid reasons biased to also exercise the 1 / 500 length boundaries."""

    return st.one_of(boundary_reasons(), valid_reasons())


def invalid_reasons() -> st.SearchStrategy[Any]:
    """Reasons rejected for being empty, whitespace-only or over 500 chars."""

    return st.one_of(
        st.none(),
        st.just(""),
        st.text(alphabet=" \t\n", min_size=1, max_size=8),
        st.text(min_size=REASON_MAX + 1, max_size=REASON_MAX + 64),
    )


# ---------------------------------------------------------------------------
# 时间范围 (time range) strategies
# ---------------------------------------------------------------------------

# Bounded window keeps generated timestamps deterministic and timezone-aware.
_MIN_DATETIME = datetime(2020, 1, 1, tzinfo=timezone.utc)
_MAX_DATETIME = datetime(2030, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def iso_timestamps() -> st.SearchStrategy[str]:
    """Timezone-aware ISO-8601 timestamps within a bounded window."""

    return st.datetimes(
        min_value=_MIN_DATETIME.replace(tzinfo=None),
        max_value=_MAX_DATETIME.replace(tzinfo=None),
        timezones=st.just(timezone.utc),
    ).map(lambda moment: moment.isoformat())


@st.composite
def ordered_timestamp_pairs(draw) -> tuple[str, str]:
    """A ``(from, to)`` pair where ``from <= to`` (valid range query)."""

    first = draw(
        st.datetimes(
            min_value=_MIN_DATETIME.replace(tzinfo=None),
            max_value=_MAX_DATETIME.replace(tzinfo=None),
            timezones=st.just(timezone.utc),
        )
    )
    delta_seconds = draw(st.integers(min_value=0, max_value=365 * 24 * 3600))
    second = first + timedelta(seconds=delta_seconds)
    if second > _MAX_DATETIME:
        second = _MAX_DATETIME
    return (first.isoformat(), second.isoformat())


@st.composite
def unordered_timestamp_pairs(draw) -> tuple[str, str]:
    """A ``(from, to)`` pair where ``from > to`` (should be rejected)."""

    later = draw(
        st.datetimes(
            min_value=_MIN_DATETIME.replace(tzinfo=None) + timedelta(seconds=1),
            max_value=_MAX_DATETIME.replace(tzinfo=None),
            timezones=st.just(timezone.utc),
        )
    )
    delta_seconds = draw(st.integers(min_value=1, max_value=365 * 24 * 3600))
    earlier = later - timedelta(seconds=delta_seconds)
    if earlier < _MIN_DATETIME:
        earlier = _MIN_DATETIME
        later = earlier + timedelta(seconds=1)
    # from = later, to = earlier => from > to
    return (later.isoformat(), earlier.isoformat())


def timestamp_pairs() -> st.SearchStrategy[tuple[str, str]]:
    """Either ordered or unordered ``(from, to)`` pairs."""

    return st.one_of(ordered_timestamp_pairs(), unordered_timestamp_pairs())


# ---------------------------------------------------------------------------
# 每套用量 (per-unit usage) strategies
# ---------------------------------------------------------------------------


def per_unit_usages() -> st.SearchStrategy[list[int]]:
    """A product's per-component per-unit usage counts.

    Each element is the number of slots (i.e. per-unit consumption) contributed
    by one component. The list length and the per-component values are kept
    small so the expanded total never exceeds ``MAX_EXPANDED_PARTS``, meaning
    ``POST /api/products`` always accepts the generated product.
    """

    return st.lists(
        st.integers(min_value=1, max_value=4),
        min_size=1,
        max_size=4,
    ).filter(lambda usage: sum(usage) <= MAX_EXPANDED_PARTS)


# ---------------------------------------------------------------------------
# Client-based builder helpers
# ---------------------------------------------------------------------------
#
# These mirror the helper style in tests/test_system.py: they drive the admin
# API via a Flask ``test_client`` and return the created resources so property
# tests can assemble a full "product + supplier stock + per-unit usage" scenario.


def _post(client, path: str, payload: dict, expected: int = 201) -> dict:
    response = client.post(path, json=payload)
    assert response.status_code == expected, response.get_json()
    return response.get_json()["data"]


def build_supplier_and_part(client, suffix: str) -> tuple[dict, dict]:
    """Create a supplier + part type pair, returning ``(supplier, part)``."""

    supplier = _post(
        client,
        "/api/suppliers",
        {"supplierCode": f"SUP-{suffix}", "name": f"供应商 {suffix}"},
    )
    part = _post(
        client,
        "/api/part-types",
        {
            "partCode": f"PART-{suffix}",
            "name": f"部件 {suffix}",
            "supplierId": supplier["id"],
            "specification": f"SPEC-{suffix}",
        },
    )
    return supplier, part


def build_supplier_inventory_batch(
    client,
    part_type_id: int,
    quantity: int,
    suffix: str,
) -> dict:
    """Create a supplier inventory batch (结存) for ``part_type_id``."""

    return _post(
        client,
        "/api/supplier-inventory-batches",
        {
            "partTypeId": part_type_id,
            "quantity": quantity,
            "batchNo": f"SIB-{suffix}",
            "productionDate": "20260101",
            "receivedDate": "20260102",
            "remarks": f"结存 {suffix}",
        },
    )


def build_product_scenario(
    client,
    suffix: str,
    usage: list[int],
    stock_per_batch: int,
) -> dict:
    """Materialise a full batch-generation scenario against the app.

    For each per-unit usage count in ``usage`` this creates a supplier + part,
    a supplier inventory batch seeded with ``stock_per_batch`` units, and a
    product component whose ``quantity`` equals that per-unit usage. The product
    is created with all components so its ACTIVE trace plan binds each supplier
    batch ``usage[i]`` times (i.e. per-unit usage == ``usage[i]``).

    Returns a dict describing the created ``product``, the ``suppliers`` /
    ``parts`` / ``inventory_batches`` lists and the ``usage`` echoed back, so
    property tests can assert inventory deductions of ``N * usage[i]``.
    """

    suppliers: list[dict] = []
    parts: list[dict] = []
    inventory_batches: list[dict] = []
    components: list[dict] = []

    for index, per_unit in enumerate(usage):
        part_suffix = f"{suffix}-{index}"
        supplier, part = build_supplier_and_part(client, part_suffix)
        inventory = build_supplier_inventory_batch(
            client, part["id"], stock_per_batch, part_suffix
        )
        suppliers.append(supplier)
        parts.append(part)
        inventory_batches.append(inventory)
        components.append(
            {"inventoryBatchId": inventory["id"], "quantity": per_unit}
        )

    product = _post(
        client,
        "/api/products",
        {"name": f"走步机 {suffix}", "components": components},
    )
    return {
        "product": product,
        "suppliers": suppliers,
        "parts": parts,
        "inventory_batches": inventory_batches,
        "usage": list(usage),
    }
