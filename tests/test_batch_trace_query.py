from __future__ import annotations

from app import create_app
from batch_strategies import build_product_scenario
from traceability.db import connect_database


def snapshot(database_path):
    database = connect_database(database_path)
    try:
        result = {}
        tables = [
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            result[table] = [tuple(row) for row in database.execute(f"SELECT * FROM {table}")]
        return result
    finally:
        database.close()


def prepared_batch(tmp_path):
    database_path = tmp_path / "batch-trace-query.db"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()
    scenario = build_product_scenario(client, "TRACE-QUERY", [2], 100)
    generated = client.post(
        "/api/production-batches",
        json={"productModelId": scenario["product"]["id"], "quantity": 6, "prefix": "TRACEQ"},
    ).get_json()["data"]
    registered = client.post(
        "/api/batch-entry/scan",
        json={"code": generated["identificationCode"], "quantity": 5},
    )
    assert registered.status_code == 201, registered.get_json()
    return client, database_path, scenario, generated


# Feature: batch-traceability, Property 29: 扫码溯源查询的只读性与字段完整性
def test_batch_trace_query_is_read_only_and_complete(tmp_path):
    client, database_path, scenario, generated = prepared_batch(tmp_path)
    before = snapshot(database_path)
    response = client.post(
        "/api/batch-trace/query", json={"code": generated["identificationCode"]}
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["batchCode"] == generated["batchCode"]
    assert data["productModelId"] == scenario["product"]["id"]
    assert data["plannedQuantity"] == 6
    assert data["registeredQuantity"] == 5
    assert data["generatedAt"] == generated["generatedAt"]
    assert data["prefix"] == "TRACEQ"
    assert data["qualityStatus"] == "ASSEMBLED"
    assert len(data["reverseTrace"]) == 1
    assert snapshot(database_path) == before


# Feature: batch-traceability, Property 31: 无效批次码查询拒绝
def test_batch_trace_query_rejects_invalid_and_unknown_codes(tmp_path):
    client, database_path, _scenario, _generated = prepared_batch(tmp_path)
    before = snapshot(database_path)
    for code in (None, "", "garbage", "PTS:B:B-NOT-FOUND"):
        response = client.post("/api/batch-trace/query", json={"code": code})
        assert response.status_code == 404
        assert "无效或不存在" in response.get_json()["message"]
    assert snapshot(database_path) == before

