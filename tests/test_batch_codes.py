"""Unit tests for batch payload encoding / parsing round-trips.

Covers task 3.2: verify ``parse_batch_payload(batch_identification_code(code))``
round-trips back to the original ``batch_code`` and that ``parse_batch_payload``
also accepts a bare ``batch_code`` (no ``PTS:B:`` prefix).

Requirements: 3.1
"""

from __future__ import annotations

import pytest

from traceability.codes import (
    batch_identification_code,
    new_batch_code,
    parse_batch_payload,
)


# A representative spread of batch codes: values produced by ``new_batch_code``
# plus hand-written codes exercising the allowed character set (letters, digits,
# underscores, hyphens) and casing.
SAMPLE_BATCH_CODES = [
    "B-LINE1-20260101120000-ABCDEF",
    "B-FACTORY_A-20250630235959-0F1E2D",
    "B-X-20240229000000-AABBCC",
    "PB-20260717-1A2B3C",
    "SIMPLE",
    "batch-code-with-lowercase",
    "MiXeD_Case-123",
]


@pytest.mark.parametrize("code", SAMPLE_BATCH_CODES)
def test_parse_of_encoded_payload_round_trips(code):
    """``parse_batch_payload(batch_identification_code(code)) == code``."""

    payload = batch_identification_code(code)
    assert payload == f"PTS:B:{code}"
    assert parse_batch_payload(payload) == code


@pytest.mark.parametrize("code", SAMPLE_BATCH_CODES)
def test_parse_accepts_bare_batch_code(code):
    """A bare ``batch_code`` (without the ``PTS:B:`` prefix) is accepted as-is."""

    assert parse_batch_payload(code) == code


def test_round_trip_for_generated_batch_code():
    """Codes minted by ``new_batch_code`` survive the encode/parse round-trip."""

    code = new_batch_code("20260101120000", "LINE1")
    assert code.startswith("B-LINE1-20260101120000-")

    assert parse_batch_payload(batch_identification_code(code)) == code
    # And the bare generated code parses to itself.
    assert parse_batch_payload(code) == code


def test_parse_strips_surrounding_whitespace():
    """Leading / trailing whitespace around the payload is trimmed."""

    code = "B-LINE1-20260101120000-ABCDEF"
    assert parse_batch_payload(f"  {batch_identification_code(code)}  ") == code
    assert parse_batch_payload(f"\t{code}\n") == code


def test_parse_strips_whitespace_between_prefix_and_code():
    """Whitespace after the ``PTS:B:`` prefix is trimmed before returning."""

    assert parse_batch_payload("PTS:B:  BATCH123  ") == "BATCH123"


@pytest.mark.parametrize("payload", [None, "", "   ", "\t\n", "PTS:B:", "PTS:B:   "])
def test_parse_rejects_empty_payloads(payload):
    """Empty / prefix-only payloads are rejected with a descriptive error."""

    with pytest.raises(ValueError, match="批次码无效"):
        parse_batch_payload(payload)
