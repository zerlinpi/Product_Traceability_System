from __future__ import annotations

from app import LEGACY_ENTRY_DISABLED_MESSAGE, create_app
from traceability.db import connect_database


def counts(database_path):
    database = connect_database(database_path)
    try:
        return {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("machines", "trace_records", "product_code_sets")
        }
    finally:
        database.close()


# Feature: batch-traceability, Property 22: 走步机旧录入入口停用且无逐台副作用
def test_treadmill_legacy_generation_and_scan_are_disabled_without_unit_rows(tmp_path):
    database_path = tmp_path / "legacy-guard.db"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()
    treadmill = next(
        item
        for item in client.get("/api/product-models").get_json()["data"]
        if item["modelCode"] == "TW-04"
    )
    before = counts(database_path)
    code_sets = client.post(
        f"/api/products/{treadmill['id']}/code-sets",
        json={"prefix": "TW04", "quantity": 1},
    )
    assert code_sets.status_code == 409
    assert code_sets.get_json()["message"] == LEGACY_ENTRY_DISABLED_MESSAGE
    scanned = client.post(
        "/api/scan",
        json={
            "stationId": "OLD-TREADMILL",
            "stationName": "旧入口",
            "productModelId": treadmill["id"],
            "code": "TW-04-NOT-CREATED",
        },
    )
    assert scanned.status_code == 409
    assert scanned.get_json()["message"] == LEGACY_ENTRY_DISABLED_MESSAGE
    assert counts(database_path) == before


def test_treadmill_active_legacy_session_cannot_be_reset_or_undone(tmp_path):
    database_path = tmp_path / "legacy-session-guard.db"
    app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    client = app.test_client()
    treadmill = next(
        item
        for item in client.get("/api/product-models").get_json()["data"]
        if item["modelCode"] == "TW-04"
    )
    machine = client.post(
        "/api/machines",
        json={
            "sn": "TW-04-HISTORY-00000001",
            "productModelId": treadmill["id"],
            "productionDate": "20260722",
        },
    ).get_json()["data"]
    database = connect_database(database_path)
    try:
        database.execute(
            """
            INSERT INTO scan_sessions(
                station_id, station_name, operator_name, machine_id,
                trace_plan_id, updated_at
            ) VALUES ('OLD-SESSION', '历史走步机工位', '历史员工', ?, NULL, '2026-07-22T10:00:00+08:00')
            """,
            (machine["id"],),
        )
    finally:
        database.close()
    for endpoint in ("reset", "undo"):
        response = client.post(
            f"/api/scan/{endpoint}", json={"stationId": "OLD-SESSION", "reason": "测试"}
        )
        assert response.status_code == 409
        assert response.get_json()["message"] == LEGACY_ENTRY_DISABLED_MESSAGE
    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT machine_id FROM scan_sessions WHERE station_id='OLD-SESSION'"
        ).fetchone()[0] == machine["id"]
    finally:
        database.close()

