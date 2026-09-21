"""Property-based tests for batch registration record queries.

These tests drive the ``GET /api/batch-trace-records`` endpoint (app.py, task
11.1) which returns batch registration records matched by generation batch
(``batchCode``) or generation-time range (Requirements 3.2, 3.3, 3.4).

Property 8 asserts the **field completeness** of each returned record: every
record carries the batch code value, product model, registered quantity,
generation time, prefix and operator, and each value is consistent with its
source — the production batch created by ``POST /api/production-batches`` and
the registration performed by ``POST /api/batch-entry/scan`` (Requirement 3.2).

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
    per_unit_usages,
    quantities_including_boundaries,
)


# Supplier inventory batches accept at most 10,000,000 units on creation, so we
# keep planned quantities within a range whose worst-case deduction
# (``quantity × max(usage)``) plus slack stays under that cap.
_MAX_PLANNED_QUANTITY = 2_000_000

# Fields Requirement 3.2 requires every returned record to carry.
_REQUIRED_FIELDS = (
    "batchCode",
    "productModelId",
    "registeredQuantity",
    "generatedAt",
    "prefix",
    "operatorName",
)


def _bounded_quantities() -> st.SearchStrategy[int]:
    """Valid planned quantities (incl. boundaries) below the stock cap."""

    return quantities_including_boundaries().filter(
        lambda value: value <= _MAX_PLANNED_QUANTITY
    )


def _operator_names() -> st.SearchStrategy[str]:
    """Non-whitespace operator names accepted by ``clean_text`` (<= 60 chars).

    The alphabet excludes whitespace so the value is unchanged by the endpoint's
    ``str.strip()`` normalisation, letting the test compare the queried
    ``operatorName`` against the exact value it submitted.
    """

    return st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789张三李四王五",
        min_size=1,
        max_size=20,
    )


def _make_registered_batch(
    client, suffix: str, usage: list[int], quantity: int, prefix: str,
    slack: int, operator_name: str, correction: int | None,
) -> dict:
    """Generate a batch then scan-register it, returning the expected source values.

    The returned dict captures the authoritative source for each Requirement 3.2
    field: the batch code / prefix / generation time / product model come from
    the ``POST /api/production-batches`` response, the operator name and
    registered quantity from the ``POST /api/batch-entry/scan`` request/response.
    """

    stock_per_batch = quantity * max(usage) + slack
    scenario = build_product_scenario(client, suffix, usage, stock_per_batch)

    generate = client.post(
        "/api/production-batches",
        json={
            "productModelId": scenario["product"]["id"],
            "quantity": quantity,
            "prefix": prefix,
        },
    )
    assert generate.status_code == 201, generate.get_json()
    created = generate.get_json()["data"]

    scan_payload: dict[str, object] = {
        "code": created["batchCode"],
        "operatorName": operator_name,
    }
    if correction is not None:
        scan_payload["quantity"] = correction

    scan = client.post("/api/batch-entry/scan", json=scan_payload)
    assert scan.status_code == 201, scan.get_json()
    registered = scan.get_json()["data"]

    expected_quantity = correction if correction is not None else created["plannedQuantity"]
    assert registered["registeredQuantity"] == expected_quantity

    return {
        "batchCode": created["batchCode"],
        "productModelId": created["productModelId"],
        "productModelCode": created["productModelCode"],
        "productName": created["productName"],
        "prefix": created["prefix"],
        "generatedAt": created["generatedAt"],
        "operatorName": operator_name.strip(),
        "registeredQuantity": expected_quantity,
    }


# Feature: batch-traceability, Property 8: 批次登记记录字段完整性
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(
    batch_count=st.integers(min_value=1, max_value=2),
    prefix=entity_code_prefixes(),
    data=st.data(),
)
def test_batch_trace_record_field_completeness(
    batch_count: int, prefix: str, data
) -> None:
    """返回结构含批次码值、产品型号、登记台数、生成时间、前缀与操作人，值与来源一致。

    Validates: Requirements 3.2
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-query-"))
    db_path = workdir / "traceability.db"
    try:
        app = create_app({"TESTING": True, "DATABASE": str(db_path)})
        client = app.test_client()

        # Generate and register a handful of batches, capturing the source
        # values each queried record must faithfully echo back.
        expected_by_code: dict[str, dict] = {}
        for index in range(batch_count):
            usage = data.draw(per_unit_usages(), label=f"usage-{index}")
            quantity = data.draw(_bounded_quantities(), label=f"quantity-{index}")
            slack = data.draw(
                st.integers(min_value=0, max_value=500), label=f"slack-{index}"
            )
            operator_name = data.draw(_operator_names(), label=f"operator-{index}")
            # Either omit the quantity (defaults to planned) or apply a valid
            # correction in ``1..planned_quantity``.
            correction = data.draw(
                st.one_of(st.none(), st.integers(min_value=1, max_value=quantity)),
                label=f"correction-{index}",
            )
            expected = _make_registered_batch(
                client,
                f"QUERY-{index}",
                usage,
                quantity,
                prefix,
                slack,
                operator_name,
                correction,
            )
            expected_by_code[expected["batchCode"]] = expected

        # Query all registered records (admin sees every record).
        response = client.get("/api/batch-trace-records")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["ok"] is True
        records = body["data"]

        # (A) Every generated+registered batch is present in the result.
        returned_codes = {record["batchCode"] for record in records}
        assert set(expected_by_code) <= returned_codes

        for record in records:
            if record["batchCode"] not in expected_by_code:
                continue
            expected = expected_by_code[record["batchCode"]]

            # (B) Every Requirement 3.2 field is present and non-empty.
            for field in _REQUIRED_FIELDS:
                assert field in record, f"missing field {field!r}"
                assert record[field] is not None and record[field] != "", (
                    f"empty field {field!r}: {record[field]!r}"
                )

            # (C) The batch code value matches its source (Requirement 3.2).
            assert record["batchCode"] == expected["batchCode"]

            # (D) The product model matches its source. The record carries the
            #     product model id, code and name from the production batch.
            assert record["productModelId"] == expected["productModelId"]
            assert record["productModelCode"] == expected["productModelCode"]
            assert record["productName"] == expected["productName"]

            # (E) The registered quantity matches the value recorded at scan time.
            assert record["registeredQuantity"] == expected["registeredQuantity"]

            # (F) The generation time matches the production batch's generatedAt.
            assert record["generatedAt"] == expected["generatedAt"]

            # (G) The prefix matches the (normalised) prefix stored on the batch.
            assert record["prefix"] == expected["prefix"]

            # (H) The operator matches the operator submitted at scan time.
            assert record["operatorName"] == expected["operatorName"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 9: 批次 / 时间范围查询的过滤与排序
# Feature: batch-traceability, Property 10: 时间范围非法拒绝
def test_batch_query_filters_inclusive_range_sorts_and_rejects_reverse_range(tmp_path):
    current = ["2026-07-22T08:00:00+08:00"]
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "query-range.db"),
            "NOW_PROVIDER": lambda: current[0],
        }
    )
    client = app.test_client()
    scenario = build_product_scenario(client, "QUERY-RANGE", [1], 20)
    created = []
    for index, timestamp in enumerate(
        [
            "2026-07-22T09:00:00+08:00",
            "2026-07-22T10:00:00+08:00",
            "2026-07-22T11:00:00+08:00",
        ]
    ):
        current[0] = timestamp
        generated = client.post(
            "/api/production-batches",
            json={
                "productModelId": scenario["product"]["id"],
                "quantity": 1,
                "prefix": f"QRANGE{index}",
            },
        )
        assert generated.status_code == 201, generated.get_json()
        batch = generated.get_json()["data"]
        assert client.post(
            "/api/batch-entry/scan", json={"code": batch["batchCode"]}
        ).status_code == 201
        created.append(batch)

    ranged = client.get(
        "/api/batch-trace-records?from=2026-07-22T09:00:00%2B08:00&to=2026-07-22T10:00:00%2B08:00"
    )
    assert ranged.status_code == 200, ranged.get_json()
    assert [row["batchCode"] for row in ranged.get_json()["data"]] == [
        created[1]["batchCode"],
        created[0]["batchCode"],
    ]
    exact = client.get(
        f"/api/batch-trace-records?batchCode={created[2]['batchCode']}"
    )
    assert [row["batchCode"] for row in exact.get_json()["data"]] == [
        created[2]["batchCode"]
    ]
    assert client.get(
        "/api/batch-trace-records?batchCode=B-NOT-FOUND"
    ).get_json()["data"] == []
    invalid = client.get(
        "/api/batch-trace-records?from=2026-07-22T12:00:00%2B08:00&to=2026-07-22T10:00:00%2B08:00"
    )
    assert invalid.status_code == 400
    assert "起始时间不能晚于结束时间" in invalid.get_json()["message"]
