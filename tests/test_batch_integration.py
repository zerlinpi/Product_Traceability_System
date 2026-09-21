from __future__ import annotations

from app import create_app
from batch_strategies import build_product_scenario
from traceability.db import connect_database


def test_batch_end_to_end_generate_register_release_and_trace(tmp_path):
    database_path = tmp_path / "batch-e2e.db"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "NOW_PROVIDER": lambda: "2026-07-22T12:00:00+08:00",
        }
    )
    client = app.test_client()
    scenario = build_product_scenario(client, "BATCH-E2E", [2], 100)
    generated = client.post(
        "/api/production-batches",
        json={
            "productModelId": scenario["product"]["id"],
            "quantity": 10,
            "prefix": "BE2E",
        },
    )
    assert generated.status_code == 201, generated.get_json()
    batch = generated.get_json()["data"]
    registered = client.post(
        "/api/batch-entry/scan",
        json={"code": batch["identificationCode"], "quantity": 9},
    )
    assert registered.status_code == 201, registered.get_json()
    record = registered.get_json()["data"]
    released = client.post(f"/api/batch-trace-records/{record['id']}/pass")
    assert released.status_code == 200, released.get_json()
    traced = client.post(
        "/api/batch-trace/query", json={"code": batch["identificationCode"]}
    )
    assert traced.status_code == 200, traced.get_json()
    data = traced.get_json()["data"]
    assert data["batchCode"] == batch["batchCode"]
    assert data["plannedQuantity"] == 10
    assert data["registeredQuantity"] == 9
    assert data["qualityStatus"] == "PASSED"
    assert data["reverseTrace"][0]["quantityConsumed"] == 20

    database = connect_database(database_path)
    try:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 20
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM batch_trace_records").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM trace_records").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 1
    finally:
        database.close()
