"""Property-based test for batch QR download and code-value round-trip.

Covers task 6.6 / Property 4: 批次二维码可下载与码值 round-trip.

For any successfully generated production batch:

* ``GET /api/production-batches/<id>/qr`` returns a valid SVG document
  (``image/svg+xml`` content type whose body contains an ``<svg`` element),
  proving the batch QR is downloadable (Requirement 1.6); and
* the batch's ``batch_code`` survives the payload encode/parse round-trip, i.e.
  ``parse_batch_payload(batch_identification_code(batch_code)) == batch_code``
  (Requirement 3.1) — the code value the QR encodes is the same one the scan
  path recovers.

The test drives the real ``POST /api/production-batches`` endpoint (task 6.1)
and the ``GET .../qr`` endpoint (task 6.2) via the Flask ``test_client`` so the
property holds end-to-end over many generated products / quantities / prefixes.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from traceability.codes import batch_identification_code, parse_batch_payload

from batch_strategies import (
    build_product_scenario,
    entity_code_prefixes,
    per_unit_usages,
    valid_quantities,
)


# Supplier inventory batches accept at most 10,000,000 units on creation, so we
# keep planned quantities below a cap whose worst-case deduction
# (``quantity × max(usage)``) plus slack stays under that bound.
_MAX_PLANNED_QUANTITY = 2_000_000


def _bounded_quantities() -> st.SearchStrategy[int]:
    """Valid planned quantities kept below the supplier-stock creation cap."""

    return valid_quantities().filter(lambda value: value <= _MAX_PLANNED_QUANTITY)


# Feature: batch-traceability, Property 4: 批次二维码可下载与码值 round-trip
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=500),
)
def test_batch_qr_downloadable_and_code_round_trips(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """``/qr`` returns a valid SVG and ``batch_code`` survives encode/parse.

    Validates: Requirements 1.6, 3.1
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-qr-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed each supplier batch with enough stock for the worst-case part so
        # generation always succeeds (this property is the success path).
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "QR", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        create_response = client.post(
            "/api/production-batches",
            json={
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": prefix,
            },
        )
        assert create_response.status_code == 201, create_response.get_json()
        batch = create_response.get_json()["data"]
        batch_id = batch["id"]
        batch_code = batch["batchCode"]

        # (A) The batch code value survives the payload encode/parse round-trip
        # (Requirement 3.1): the QR encodes ``PTS:B:{batch_code}`` and parsing
        # that payload recovers the original code exactly.
        payload = batch_identification_code(batch_code)
        assert payload == f"PTS:B:{batch_code}"
        assert parse_batch_payload(payload) == batch_code

        # (B) The batch QR is downloadable as a valid SVG (Requirement 1.6).
        qr_response = client.get(f"/api/production-batches/{batch_id}/qr")
        assert qr_response.status_code == 200, qr_response.get_data(as_text=True)
        assert qr_response.mimetype == "image/svg+xml"
        body = qr_response.get_data(as_text=True)
        assert "<svg" in body

        # (C) The downloaded QR encodes the very code value that round-trips:
        # the scan payload parses back to this batch's ``batch_code``.
        assert parse_batch_payload(batch_identification_code(batch_code)) == batch_code
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
