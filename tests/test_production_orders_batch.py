from __future__ import annotations

from test_inbound_receipts import create_order
from test_purchase_orders import setup_case
from traceability.codes import parse_batch_payload
from traceability.db import connect_database


def batch_generate(client, purchase_order_ids, external=False):
    response = client.post(
        "/api/production-orders/batch",
        json={"purchaseOrderIds": purchase_order_ids, "external": external},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# Warehouse batch-generates production orders from operations' purchase orders,
# minting one unique trace/QR code per order and skipping already-generated ones.
def test_batch_generation_creates_one_order_per_po_and_skips_generated(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    first = create_order(client, scenario, 8)
    second = create_order(client, scenario, 9)
    third = create_order(client, scenario, 10)

    # Pre-generate the first order via the single endpoint so the batch call
    # must report it as skipped rather than erroring or duplicating it.
    single = client.post(
        "/api/production-orders", json={"purchaseOrderId": first["id"]}
    )
    assert single.status_code == 201, single.get_json()

    result = batch_generate(client, [first["id"], second["id"], third["id"]])
    created = result["created"]
    skipped = result["skipped"]
    assert result["failed"] == []
    assert len(created) == 2
    assert {item["purchaseOrderId"] for item in created} == {second["id"], third["id"]}
    assert [item["purchaseOrderId"] for item in skipped] == [first["id"]]
    # Every generated order carries its own unique production QR code.
    codes = {item["productionQrCode"] for item in created}
    assert len(codes) == 2
    for item in created:
        assert item["isExternal"] is False
        assert parse_batch_payload(item["identificationCode"]) == item["productionQrCode"]

    database = connect_database(database_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0] == 3
        assert database.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0] == 3
        assert database.execute(
            "SELECT COUNT(DISTINCT batch_code) FROM production_batches"
        ).fetchone()[0] == 3
        assert database.execute(
            "SELECT COUNT(*) FROM production_orders WHERE is_external = 1"
        ).fetchone()[0] == 0
    finally:
        database.close()


# Marking the batch as 外采 (external procurement) still mints the trace code but
# flags every generated order as external.
def test_batch_generation_external_flags_every_order(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    first = create_order(client, scenario, 5)
    second = create_order(client, scenario, 6)

    result = batch_generate(client, [first["id"], second["id"]], external=True)
    assert len(result["created"]) == 2
    assert all(item["isExternal"] is True for item in result["created"])

    listed = client.get("/api/production-orders").get_json()["data"]
    assert all(item["isExternal"] is True for item in listed)

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT COUNT(*) FROM production_orders WHERE is_external = 1"
        ).fetchone()[0] == 2
    finally:
        database.close()


# A single unknown / duplicate id is reported per-order; the transaction of each
# other order is unaffected (one failure does not abort the whole batch).
def test_batch_generation_reports_unknown_ids_without_aborting_others(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    good = create_order(client, scenario, 7)

    result = batch_generate(client, [good["id"], 999999])
    assert [item["purchaseOrderId"] for item in result["created"]] == [good["id"]]
    assert result["skipped"] == []
    assert len(result["failed"]) == 1
    assert result["failed"][0]["purchaseOrderId"] == 999999


def test_batch_generation_requires_a_non_empty_selection(tmp_path):
    client, _database_path, _scenario, _fake = setup_case(tmp_path)
    assert client.post("/api/production-orders/batch", json={}).status_code == 400
    assert client.post(
        "/api/production-orders/batch", json={"purchaseOrderIds": []}
    ).status_code == 400


# The single create endpoint also accepts the external flag.
def test_single_create_accepts_external_flag(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 4)
    response = client.post(
        "/api/production-orders",
        json={"purchaseOrderId": order["id"], "external": True},
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["isExternal"] is True
    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT is_external FROM production_orders WHERE purchase_order_id = ?",
            (order["id"],),
        ).fetchone()[0] == 1
    finally:
        database.close()


# The production-batches list carries per-batch registration status so the
# warehouse batch-generation list shows product name + 未登记/质量状态 correctly
# (the fix for "batch registered but shows 未登记").
def test_production_batches_list_exposes_registration_fields(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 6)
    client.post("/api/production-orders", json={"purchaseOrderId": order["id"]})

    batches = client.get("/api/production-batches").get_json()["data"]
    assert len(batches) == 1
    batch = batches[0]
    assert batch["productName"] == scenario["product"]["name"]
    assert batch["registered"] is False
    assert batch["registeredQuantity"] is None
    assert batch["qualityStatus"] is None
