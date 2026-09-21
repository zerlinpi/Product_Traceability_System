"""Property-based tests for the batch-level quality state machine.

These tests drive the batch quality endpoints (app.py, task 10.1):

* ``POST /api/batch-trace-records/<id>/pass`` — ADMIN releases a batch
  registration record (ASSEMBLED -> PASSED, Requirement 6.2); and
* ``POST /api/batch-trace-records/<id>/hold`` — ADMIN holds a record
  (ASSEMBLED -> HOLD with a reason).

Property 19 asserts the **合格转换状态机 (pass transition state machine)**:

* a record currently in 待检 (ASSEMBLED) transitions to 合格 (PASSED) on a
  release (Requirement 6.2); and
* performing a release or a hold on a record that is **not** in ASSEMBLED
  (i.e. already PASSED or HOLD) is rejected and leaves the quality status
  unchanged (Requirement 6.5).

The scenario (product + supplier stock + per-unit usage) is assembled via the
admin API using the shared builders in ``tests/batch_strategies.py`` following
the ``tests/test_batch_entry.py`` conventions for app / DB setup.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from traceability.db import connect_database

from batch_strategies import (
    build_product_scenario,
    entity_code_prefixes,
    invalid_reasons,
    per_unit_usages,
    quantities_including_boundaries,
    reasons_including_boundaries,
)


# Supplier inventory batches accept at most 10,000,000 units on creation. This
# test generates *two* production batches from one product scenario, so keep the
# per-batch worst-case deduction (``quantity × max(usage)``) comfortably under
# that cap even when doubled.
_MAX_PLANNED_QUANTITY = 1_000_000


def _bounded_quantities() -> st.SearchStrategy[int]:
    """Valid planned quantities (incl. boundaries) below the stock cap."""

    return quantities_including_boundaries().filter(
        lambda value: value <= _MAX_PLANNED_QUANTITY
    )


def _generate_and_register(client, product_model_id: int, quantity: int, prefix: str) -> int:
    """Generate a production batch, scan-register it and return the record id."""

    generate = client.post(
        "/api/production-batches",
        json={
            "productModelId": product_model_id,
            "quantity": quantity,
            "prefix": prefix,
        },
    )
    assert generate.status_code == 201, generate.get_json()
    batch_code = generate.get_json()["data"]["batchCode"]

    scan = client.post("/api/batch-entry/scan", json={"code": batch_code})
    assert scan.status_code == 201, scan.get_json()
    return scan.get_json()["data"]["id"]


def _quality_status(db_path: Path, record_id: int) -> str:
    connection = connect_database(str(db_path))
    try:
        row = connection.execute(
            "SELECT quality_status FROM batch_trace_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        assert row is not None
        return row["quality_status"]
    finally:
        connection.close()


def _status_reason(db_path: Path, record_id: int) -> str | None:
    connection = connect_database(str(db_path))
    try:
        row = connection.execute(
            "SELECT status_reason FROM batch_trace_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        assert row is not None
        return row["status_reason"]
    finally:
        connection.close()


# Feature: batch-traceability, Property 19: 合格转换状态机
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    reason=reasons_including_boundaries(),
    attempt=st.sampled_from(["pass", "hold"]),
    slack=st.integers(min_value=0, max_value=500),
)
def test_pass_transition_state_machine(
    usage: list[int],
    quantity: int,
    prefix: str,
    reason: str,
    attempt: str,
    slack: int,
) -> None:
    """ASSEMBLED → PASSED；非待检状态执行合格 / 暂扣被拒且状态不变。

    Validates: Requirements 6.2, 6.5
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-quality-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Seed each supplier batch with enough stock for TWO batches (one for
        # the PASSED pre-state, one for the HOLD pre-state) so both generations
        # succeed.
        stock_per_batch = 2 * quantity * max(usage) + slack
        scenario = build_product_scenario(client, "QUALITY", usage, stock_per_batch)
        product_model_id = scenario["product"]["id"]

        # ---- Part 1: ASSEMBLED -> PASSED release succeeds (Requirement 6.2) ----
        passed_record_id = _generate_and_register(
            client, product_model_id, quantity, prefix
        )
        assert _quality_status(db_path, passed_record_id) == "ASSEMBLED"

        release = client.post(f"/api/batch-trace-records/{passed_record_id}/pass")
        assert release.status_code == 200, release.get_json()
        body = release.get_json()
        assert body["ok"] is True
        assert body["data"]["qualityStatus"] == "PASSED"
        assert _quality_status(db_path, passed_record_id) == "PASSED"

        # ---- Part 2: reach a HOLD pre-state on a second record ----
        held_record_id = _generate_and_register(
            client, product_model_id, quantity, f"{prefix[:31]}H"
        )
        hold = client.post(
            f"/api/batch-trace-records/{held_record_id}/hold",
            json={"reason": reason},
        )
        assert hold.status_code == 200, hold.get_json()
        assert hold.get_json()["data"]["qualityStatus"] == "HOLD"
        assert _quality_status(db_path, held_record_id) == "HOLD"

        # ---- Part 3: pass / hold on a non-ASSEMBLED record is rejected and the
        #      quality status is left unchanged (Requirement 6.5) ----
        for record_id, pre_state in (
            (passed_record_id, "PASSED"),
            (held_record_id, "HOLD"),
        ):
            if attempt == "pass":
                response = client.post(
                    f"/api/batch-trace-records/{record_id}/pass"
                )
            else:
                response = client.post(
                    f"/api/batch-trace-records/{record_id}/hold",
                    json={"reason": reason},
                )
            # The transition is rejected (non-待检 state) ...
            assert response.status_code == 409, response.get_json()
            assert response.get_json()["ok"] is False
            # ... and the quality status is unchanged.
            assert _quality_status(db_path, record_id) == pre_state
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 20: 暂扣转换与原因长度校验
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    reason=st.one_of(reasons_including_boundaries(), invalid_reasons()),
    slack=st.integers(min_value=0, max_value=500),
)
def test_hold_transition_and_reason_length_validation(
    usage: list[int],
    quantity: int,
    prefix: str,
    reason: object,
    slack: int,
) -> None:
    """`1..500` 原因暂扣成功并原样保存；空或超长拒绝且状态不变。

    A hold with a reason whose (trimmed) length is within ``[1, 500]`` moves the
    record ASSEMBLED -> HOLD and persists the reason verbatim in
    ``status_reason`` (Requirement 6.3). An empty / whitespace-only / over-500
    reason is rejected with a descriptive error and the quality status is left
    unchanged at ASSEMBLED (Requirement 6.4).

    Validates: Requirements 6.3, 6.4
    """

    # Mirror the endpoint's normalization: it holds with ``str(reason or "").
    # strip()`` and accepts only when the trimmed length is within [1, 500].
    normalized = str(reason or "").strip()
    should_accept = 1 <= len(normalized) <= 500

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-hold-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # A fresh batch is registered once per example so every case holds a
        # pristine ASSEMBLED record.
        stock_per_batch = quantity * max(usage) + slack
        scenario = build_product_scenario(client, "HOLD", usage, stock_per_batch)
        record_id = _generate_and_register(
            client, scenario["product"]["id"], quantity, prefix
        )
        assert _quality_status(db_path, record_id) == "ASSEMBLED"

        response = client.post(
            f"/api/batch-trace-records/{record_id}/hold",
            json={"reason": reason},
        )

        if should_accept:
            # ASSEMBLED -> HOLD with the reason saved verbatim (Requirement 6.3).
            assert response.status_code == 200, response.get_json()
            body = response.get_json()
            assert body["ok"] is True
            assert body["data"]["qualityStatus"] == "HOLD"
            assert body["data"]["statusReason"] == normalized
            assert _quality_status(db_path, record_id) == "HOLD"
            assert _status_reason(db_path, record_id) == normalized
        else:
            # Empty / whitespace-only / over-500 reason: rejected with a
            # descriptive error and the status is unchanged (Requirement 6.4).
            assert response.status_code == 400, response.get_json()
            payload = response.get_json()
            assert payload["ok"] is False
            assert payload["message"]
            assert _quality_status(db_path, record_id) == "ASSEMBLED"
            assert _status_reason(db_path, record_id) in (None, "")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 21: 质量状态变更写审计事件
def test_quality_transition_writes_one_complete_audit_event(tmp_path):
    import json

    db_path = tmp_path / "quality-audit.db"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(db_path),
            "NOW_PROVIDER": lambda: "2026-07-22T11:00:00+08:00",
        }
    )
    client = app.test_client()
    scenario = build_product_scenario(client, "QUALITY-AUDIT", [1], 10)
    record_id = _generate_and_register(client, scenario["product"]["id"], 2, "QAPASS")
    response = client.post(f"/api/batch-trace-records/{record_id}/pass")
    assert response.status_code == 200, response.get_json()

    connection = connect_database(str(db_path))
    try:
        events = connection.execute(
            """
            SELECT event_type, object_type, object_code, operator_name,
                   occurred_at, reason, payload_json
            FROM audit_events
            WHERE event_type='BATCH_TRACE_RECORD_STATUS_CHANGED'
            """
        ).fetchall()
        assert len(events) == 1
        event = events[0]
        assert event["object_type"] == "BATCH_TRACE_RECORD"
        assert event["object_code"]
        assert event["operator_name"]
        assert event["occurred_at"] == "2026-07-22T11:00:00+08:00"
        assert json.loads(event["payload_json"]) == {
            "fromStatus": "ASSEMBLED",
            "toStatus": "PASSED",
        }
    finally:
        connection.close()
