from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from app import create_app
from traceability.ble_collector import _extract_identity


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "traceability-test.db"),
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def post_json(client, path: str, payload: dict, expected: int = 201):
    response = client.post(path, json=payload)
    assert response.status_code == expected, response.get_json()
    return response.get_json()["data"]


def create_supplier_and_part(client, suffix: str):
    supplier = post_json(
        client,
        "/api/suppliers",
        {"supplierCode": f"SUP-{suffix}", "name": f"供应商 {suffix}"},
    )
    part = post_json(
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


def create_labels(client, part_id: int, quantity: int):
    return post_json(
        client,
        "/api/part-labels",
        {
            "partTypeId": part_id,
            "lotNo": "LOT-20260716",
            "supplierBatchNo": "SUP-BATCH-01",
            "quantity": quantity,
        },
    )


def ensure_legacy_unit_model(client):
    models = client.get("/api/product-models").get_json()["data"]
    existing = next((item for item in models if item["modelCode"] == "LEGACY-UNIT"), None)
    if existing:
        return existing
    families = client.get("/api/product-families").get_json()["data"]
    family = next((item for item in families if item["productCode"] == "LEGACY_UNIT"), None)
    if family is None:
        family = post_json(
            client,
            "/api/product-families",
            {"productCode": "LEGACY_UNIT", "name": "通用逐台产品"},
        )
    return post_json(
        client,
        "/api/product-models",
        {
            "productFamilyId": family["id"],
            "modelCode": "LEGACY-UNIT",
            "name": "通用逐台产品",
            "serialPrefix": "TW-04",
            "identitySource": "SCANNER",
        },
    )


def create_machine(client, serial: str):
    model = ensure_legacy_unit_model(client)
    return post_json(
        client,
        "/api/machines",
        {"sn": serial, "productModelId": model["id"], "productionDate": "20260716"},
    )


def create_trace_plan(client, version: str, slots: list[dict], model: str = "LEGACY-UNIT"):
    if model == "LEGACY-UNIT":
        ensure_legacy_unit_model(client)
    return post_json(
        client,
        "/api/trace-plans",
        {
            "modelCode": model,
            "name": f"{model} 生产 BOM",
            "version": version,
            "slots": slots,
        },
    )


def scan(client, station: str, code: str, expected: int = 200):
    response = client.post(
        "/api/scan",
        json={
            "stationId": station,
            "stationName": station,
            "operatorName": f"OP-{station[-1]}",
            "code": code,
        },
    )
    assert response.status_code == expected, response.get_json()
    return response.get_json()


def test_machine_identity_is_unique_and_qr_is_generated(client):
    machine = create_machine(client, "TW-04-AAAAAAAAAAAAAAAAAAAAAAAA")
    assert machine["identificationCode"] == "PTS:M:TW-04-AAAAAAAAAAAAAAAAAAAAAAAA"
    assert machine["productionDate"] == "20260716"

    duplicate = client.post(
        "/api/machines",
        json={"sn": machine["sn"], "model": "TW-04"},
    )
    assert duplicate.status_code == 409

    qr = client.get(f"/api/machines/{machine['id']}/qr")
    assert qr.status_code == 200
    assert qr.mimetype == "image/svg+xml"
    assert b"<svg" in qr.data


def test_simplified_product_flow_generates_bound_unique_code_sets_and_supports_record_correction(client):
    product_response = client.post(
        "/api/products",
        json={
            "name": "升降桌控制器",
            "components": [
                {"name": "控制板", "supplierName": "控制板供应商", "quantity": 1},
                {"name": "线束", "supplierName": "线束供应商", "quantity": 2},
            ],
        },
    )
    assert product_response.status_code == 201, product_response.get_json()
    product = product_response.get_json()["data"]
    assert product["name"] == "升降桌控制器"
    assert product["productCode"].startswith("PRD-")
    assert product["componentCount"] == 3
    assert [item["slotName"] for item in product["components"]] == ["控制板", "线束 1", "线束 2"]

    generated_response = client.post(
        f"/api/products/{product['id']}/code-sets",
        json={"prefix": "TABLE-A", "quantity": 2},
    )
    assert generated_response.status_code == 201, generated_response.get_json()
    generated = generated_response.get_json()["data"]
    assert len(generated) == 2
    assert generated[0]["setCode"].endswith("-0001")
    assert generated[1]["setCode"].endswith("-0002")
    all_codes = [
        code
        for code_set in generated
        for code in [code_set["machine"]["identificationCode"]]
        + [part["identificationCode"] for part in code_set["parts"]]
    ]
    assert len(all_codes) == len(set(all_codes)) == 8
    assert all(len(code_set["parts"]) == 3 for code_set in generated)

    archive = client.get(generated[0]["downloadUrl"])
    assert archive.status_code == 200
    with ZipFile(BytesIO(archive.data)) as zipped:
        assert "00-产品主码.svg" in zipped.namelist()
        assert "二维码清单.csv" in zipped.namelist()
        assert len([name for name in zipped.namelist() if name.endswith(".svg")]) == 4

    station = "SIMPLE-PRODUCT-STATION"
    main = generated[0]
    other = generated[1]
    first = client.post(
        "/api/scan",
        json={
            "stationId": station,
            "stationName": "自动录入终端",
            "productModelId": product["id"],
            "code": main["machine"]["identificationCode"],
        },
    )
    assert first.status_code == 200

    mismatched = client.post(
        "/api/scan",
        json={
            "stationId": station,
            "stationName": "自动录入终端",
            "productModelId": product["id"],
            "code": other["parts"][0]["identificationCode"],
        },
    )
    assert mismatched.status_code == 409
    assert "不对应" in mismatched.get_json()["message"]

    scanned = client.post(
        "/api/scan",
        json={
            "stationId": station,
            "stationName": "自动录入终端",
            "productModelId": product["id"],
            "code": main["parts"][0]["identificationCode"],
        },
    )
    assert scanned.status_code == 200
    duplicate = client.post(
        "/api/scan",
        json={
            "stationId": station,
            "stationName": "自动录入终端",
            "productModelId": product["id"],
            "code": main["parts"][0]["identificationCode"],
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.get_json()["message"].startswith("扫描重复，请检查")
    for part in main["parts"][1:]:
        completed = client.post(
            "/api/scan",
            json={
                "stationId": station,
                "stationName": "自动录入终端",
                "productModelId": product["id"],
                "code": part["identificationCode"],
            },
        )
    assert completed.status_code == 200
    assert completed.get_json()["data"]["completed"] is True
    record = completed.get_json()["data"]["record"]

    corrected = client.put(
        f"/api/records/{record['id']}",
        json={
            "partCodes": [part["identificationCode"] for part in main["parts"]],
            "remarks": "现场校对完成",
        },
    )
    assert corrected.status_code == 200, corrected.get_json()
    assert corrected.get_json()["data"]["remarks"] == "现场校对完成"
    removed = client.delete(
        f"/api/records/{record['id']}", json={"reason": "现场校对后重新录入"}
    )
    assert removed.status_code == 200
    assert client.get(f"/api/records?productModelId={product['id']}").get_json()["data"] == []


def test_supplier_inventory_drives_product_batches_edits_and_bulk_downloads(client):
    supplier, part = create_supplier_and_part(client, "INVENTORY")
    inventory = post_json(
        client,
        "/api/supplier-inventory-batches",
        {
            "partTypeId": part["id"],
            "batchNo": "LOT-INV-001",
            "quantity": 10,
            "productionDate": "2026-07-10",
            "receivedDate": "2026-07-18",
            "remarks": "首批到货",
        },
    )
    assert inventory["quantityReceived"] == 10
    assert inventory["quantityAvailable"] == 10
    assert inventory["quantityConsumed"] == 0

    product_response = client.post(
        "/api/products",
        json={
            "name": "库存联动产品",
            "components": [{"inventoryBatchId": inventory["id"], "quantity": 2}],
        },
    )
    assert product_response.status_code == 201, product_response.get_json()
    product = product_response.get_json()["data"]
    assert product["availableUnits"] == 5
    assert product["components"][0]["inventoryBatchId"] == inventory["id"]
    assert product["components"][0]["inventoryBatchNo"] == "LOT-INV-001"

    generated_response = client.post(
        f"/api/products/{product['id']}/code-sets",
        json={"prefix": "INV-PROD", "quantity": 3},
    )
    assert generated_response.status_code == 201, generated_response.get_json()
    generated = generated_response.get_json()["data"]
    assert len(generated) == 3
    generation_batch_id = generated[0]["generationBatchId"]
    assert generation_batch_id
    assert {item["generationBatchId"] for item in generated} == {generation_batch_id}

    refreshed_inventory = client.get(
        f"/api/supplier-inventory-batches?partTypeId={part['id']}"
    ).get_json()["data"][0]
    assert refreshed_inventory["quantityConsumed"] == 6
    assert refreshed_inventory["quantityAvailable"] == 4
    movements = client.get(
        f"/api/supplier-inventory-batches/{inventory['id']}/movements"
    ).get_json()["data"]
    assert [item["movementType"] for item in movements[:2]] == ["ISSUE", "RECEIPT"]
    assert movements[0]["quantityChange"] == -6

    batches = client.get(
        f"/api/product-code-batches?productModelId={product['id']}"
    ).get_json()["data"]
    assert len(batches) == 1
    assert batches[0]["quantity"] == 3
    detail = client.get(
        f"/api/product-code-batches/{generation_batch_id}?page=1&pageSize=10"
    ).get_json()["data"]
    assert detail["pagination"]["total"] == 3
    assert len(detail["sets"]) == 3

    batch_archive = client.get(batches[0]["downloadUrl"])
    assert batch_archive.status_code == 200
    with ZipFile(BytesIO(batch_archive.data)) as archive:
        names = archive.namelist()
        assert len([name for name in names if name.endswith(".svg")]) == 9
        assert "二维码清单.csv" in names
    product_archive = client.get(f"/api/products/{product['id']}/qrcodes.zip")
    assert product_archive.status_code == 200
    with ZipFile(BytesIO(product_archive.data)) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".svg")]) == 9

    too_small = client.put(
        f"/api/supplier-inventory-batches/{inventory['id']}",
        json={"quantity": 5},
    )
    assert too_small.status_code == 409
    adjusted = client.put(
        f"/api/supplier-inventory-batches/{inventory['id']}",
        json={
            "batchNo": "LOT-INV-001A",
            "quantity": 12,
            "productionDate": "2026-07-10",
            "receivedDate": "2026-07-18",
        },
    )
    assert adjusted.status_code == 200, adjusted.get_json()
    assert adjusted.get_json()["data"]["quantityConsumed"] == 6
    assert adjusted.get_json()["data"]["quantityAvailable"] == 6

    updated_product = client.put(
        f"/api/products/{product['id']}",
        json={
            "name": "库存联动产品 V2",
            "components": [{"inventoryBatchId": inventory["id"], "quantity": 1}],
        },
    )
    assert updated_product.status_code == 200, updated_product.get_json()
    assert updated_product.get_json()["data"]["name"] == "库存联动产品 V2"
    assert updated_product.get_json()["data"]["availableUnits"] == 6
    assert updated_product.get_json()["data"]["generationBatchCount"] == 1

    supplier_update = client.put(
        f"/api/suppliers/{supplier['id']}",
        json={"supplierCode": supplier["supplierCode"], "name": "库存供应商（已编辑）"},
    )
    assert supplier_update.status_code == 200
    part_update = client.put(
        f"/api/part-types/{part['id']}",
        json={"partCode": part["partCode"], "name": "库存部件（已编辑）", "specification": "SPEC-V2"},
    )
    assert part_update.status_code == 200
    supplier_detail = client.get(f"/api/suppliers/{supplier['id']}").get_json()["data"]
    assert supplier_detail["supplier"]["name"] == "库存供应商（已编辑）"
    assert supplier_detail["parts"][0]["name"] == "库存部件（已编辑）"
    assert supplier_detail["batches"][0]["batchNo"] == "LOT-INV-001A"
    assert supplier_detail["usedInProducts"][0]["id"] == product["id"]
    assert supplier_detail["usedInProducts"][0]["requiredQuantity"] == 1
    assert supplier_detail["usedInProducts"][0]["parts"][0]["id"] == part["id"]

    invalid_quantity = client.post(
        f"/api/products/{product['id']}/code-sets",
        json={"prefix": "INV-PROD", "quantity": -1},
    )
    assert invalid_quantity.status_code == 400
    assert "至少为 1" in invalid_quantity.get_json()["message"]


def test_unused_product_is_deletable_but_used_product_is_protected(client):
    # Create both products up front (no deletion in between) so the auto-created
    # supplier/part codes stay unique.
    used = client.post(
        "/api/products",
        json={
            "name": "已用产品",
            "components": [{"name": "主板", "supplierName": "主板供应商", "quantity": 1}],
        },
    ).get_json()["data"]
    unused = client.post(
        "/api/products",
        json={
            "name": "待删除产品",
            "components": [{"name": "外壳", "supplierName": "外壳供应商", "quantity": 1}],
        },
    ).get_json()["data"]

    generated = client.post(
        f"/api/products/{used['id']}/code-sets",
        json={"prefix": "USED-A", "quantity": 1},
    )
    assert generated.status_code == 201, generated.get_json()

    # A product that already generated codes cannot be deleted.
    blocked = client.delete(f"/api/product-models/{used['id']}")
    assert blocked.status_code == 409
    assert "无法删除" in blocked.get_json()["message"]

    # An unused product can be deleted and disappears from the list.
    deleted = client.delete(f"/api/product-models/{unused['id']}")
    assert deleted.status_code == 200, deleted.get_json()
    remaining = client.get("/api/products").get_json()["data"]
    assert all(item["id"] != unused["id"] for item in remaining)

    missing = client.delete("/api/product-models/999999")
    assert missing.status_code == 404


def test_dashboard_stock_alerts_and_quality_status_form_a_closed_loop(client):
    supplier, part = create_supplier_and_part(client, "QUALITY-LOOP")
    updated_part = client.put(
        f"/api/part-types/{part['id']}",
        json={
            "partCode": part["partCode"],
            "name": part["name"],
            "specification": part["specification"],
            "minimumStock": 8,
        },
    )
    assert updated_part.status_code == 200, updated_part.get_json()
    assert updated_part.get_json()["data"]["minimumStock"] == 8

    inventory = post_json(
        client,
        "/api/supplier-inventory-batches",
        {
            "partTypeId": part["id"],
            "batchNo": "LOT-QC-001",
            "quantity": 5,
            "receivedDate": "2026-07-19",
        },
    )
    product = post_json(
        client,
        "/api/products",
        {
            "name": "质量闭环产品",
            "components": [{"inventoryBatchId": inventory["id"], "quantity": 1}],
        },
    )
    generated = post_json(
        client,
        f"/api/products/{product['id']}/code-sets",
        {"prefix": "QC-LOOP", "quantity": 1},
    )[0]
    scan(client, "STATION-QC", generated["machine"]["identificationCode"])
    completed = scan(client, "STATION-QC", generated["parts"][0]["identificationCode"])
    assert completed["data"]["completed"] is True
    record = completed["data"]["record"]
    assert record["status"] == "ASSEMBLED"

    dashboard = client.get("/api/dashboard").get_json()["data"]
    assert dashboard["counts"]["assembledRecords"] == 1
    assert dashboard["counts"]["lowStockParts"] == 1
    assert dashboard["counts"]["outOfStockParts"] == 0
    assert dashboard["stockAlerts"][0]["partCode"] == part["partCode"]
    assert dashboard["stockAlerts"][0]["quantityAvailable"] == 4
    assert dashboard["stockAlerts"][0]["stockStatus"] == "LOW"
    assert dashboard["qualityQueue"][0]["id"] == record["id"]

    missing_reason = client.put(
        f"/api/records/{record['id']}/status", json={"status": "HOLD"}
    )
    assert missing_reason.status_code == 400
    held = client.put(
        f"/api/records/{record['id']}/status",
        json={"status": "HOLD", "reason": "端子压接外观待复核"},
    )
    assert held.status_code == 200, held.get_json()
    assert held.get_json()["data"]["status"] == "HOLD"
    assert held.get_json()["data"]["statusReason"] == "端子压接外观待复核"
    held_records = client.get(
        f"/api/records?productModelId={product['id']}&status=HOLD"
    ).get_json()["data"]
    assert [item["id"] for item in held_records] == [record["id"]]

    corrected = client.put(
        f"/api/records/{record['id']}",
        json={
            "partCodes": [generated["parts"][0]["identificationCode"]],
            "remarks": "复核后重新提交",
        },
    )
    assert corrected.status_code == 200, corrected.get_json()
    assert corrected.get_json()["data"]["status"] == "ASSEMBLED"
    assert corrected.get_json()["data"]["statusReason"] == ""

    passed = client.put(
        f"/api/records/{record['id']}/status",
        json={"status": "PASSED", "reason": "外观复检合格"},
    )
    assert passed.status_code == 200, passed.get_json()
    assert passed.get_json()["data"]["status"] == "PASSED"
    final_dashboard = client.get("/api/dashboard").get_json()["data"]
    assert final_dashboard["counts"]["assembledRecords"] == 0
    assert final_dashboard["counts"]["holdRecords"] == 0
    assert final_dashboard["counts"]["passedRecords"] == 1
    audit = client.get(
        "/api/audit-events?eventType=TRACE_RECORD_STATUS_CHANGED"
    ).get_json()["data"]
    assert audit["filteredTotal"] == 2


def test_trace_records_support_batch_date_filters_bulk_quality_and_genealogy_qr(client, app):
    _, part = create_supplier_and_part(client, "TRACE-FILTER")
    inventory = post_json(
        client,
        "/api/supplier-inventory-batches",
        {
            "partTypeId": part["id"],
            "batchNo": "LOT-TRACE-001",
            "quantity": 4,
            "receivedDate": "2026-07-19",
        },
    )
    product = post_json(
        client,
        "/api/products",
        {
            "name": "批次日期筛选产品",
            "components": [{"inventoryBatchId": inventory["id"], "quantity": 1}],
        },
    )
    first_set = post_json(
        client,
        f"/api/products/{product['id']}/code-sets",
        {"prefix": "TRACE-A", "quantity": 1},
    )[0]
    second_set = post_json(
        client,
        f"/api/products/{product['id']}/code-sets",
        {"prefix": "TRACE-B", "quantity": 1},
    )[0]

    completed_records = []
    for index, code_set in enumerate((first_set, second_set), start=1):
        station = f"STATION-TRACE-{index}"
        scan(client, station, code_set["machine"]["identificationCode"])
        completed = scan(client, station, code_set["parts"][0]["identificationCode"])
        completed_records.append(completed["data"]["record"])

    with sqlite3.connect(app.config["DATABASE"]) as database:
        database.execute(
            "UPDATE trace_records SET completed_at = ? WHERE id = ?",
            ("2026-07-18T09:00:00+08:00", completed_records[0]["id"]),
        )
        database.execute(
            "UPDATE trace_records SET completed_at = ? WHERE id = ?",
            ("2026-07-19T10:00:00+08:00", completed_records[1]["id"]),
        )

    first_batch_records = client.get(
        f"/api/records?productModelId={product['id']}&generationBatchId={first_set['generationBatchId']}"
    ).get_json()["data"]
    assert [item["id"] for item in first_batch_records] == [completed_records[0]["id"]]
    assert first_batch_records[0]["generationBatch"]["id"] == first_set["generationBatchId"]

    date_records = client.get(
        f"/api/records?productModelId={product['id']}&dateFrom=2026-07-19&dateTo=2026-07-19"
    ).get_json()["data"]
    assert [item["id"] for item in date_records] == [completed_records[1]["id"]]
    invalid_range = client.get(
        f"/api/records?productModelId={product['id']}&dateFrom=2026-07-20&dateTo=2026-07-19"
    )
    assert invalid_range.status_code == 400

    bulk = client.put(
        "/api/records/status/bulk",
        json={
            "recordIds": [item["id"] for item in completed_records],
            "status": "PASSED",
            "reason": "批量终检合格",
        },
    )
    assert bulk.status_code == 200, bulk.get_json()
    assert bulk.get_json()["data"]["count"] == 2
    assert {item["status"] for item in bulk.get_json()["data"]["records"]} == {"PASSED"}

    genealogy = client.get(
        f"/api/genealogy?code={first_set['machine']['identificationCode']}"
    ).get_json()["data"]
    assert genealogy["machine"]["qrUrl"].endswith(f"/{first_set['machine']['id']}/qr")
    assert genealogy["records"][0]["parts"][0]["qrUrl"].endswith(
        f"/{first_set['parts'][0]['partLabelId']}/qr"
    )


def test_product_code_generation_accepts_one_thousand_sets(client):
    _, part = create_supplier_and_part(client, "THOUSAND")
    inventory = post_json(
        client,
        "/api/supplier-inventory-batches",
        {
            "partTypeId": part["id"],
            "batchNo": "LOT-1000",
            "quantity": 1000,
            "receivedDate": "2026-07-18",
        },
    )
    product = post_json(
        client,
        "/api/products",
        {
            "name": "千套生成验证产品",
            "components": [{"inventoryBatchId": inventory["id"], "quantity": 1}],
        },
    )
    response = client.post(
        f"/api/products/{product['id']}/code-sets",
        json={"prefix": "LOAD-1000", "quantity": 1000},
    )
    assert response.status_code == 201, response.get_json()
    generated = response.get_json()["data"]
    assert len(generated) == 1000
    assert generated[0]["setCode"].endswith("-0001")
    assert generated[-1]["setCode"].endswith("-1000")
    assert len({item["machine"]["identificationCode"] for item in generated}) == 1000
    assert {item["generationBatchId"] for item in generated} == {
        generated[0]["generationBatchId"]
    }
    refreshed = client.get(
        f"/api/supplier-inventory-batches?partTypeId={part['id']}"
    ).get_json()["data"][0]
    assert refreshed["quantityAvailable"] == 0
    batch = client.get(
        f"/api/product-code-batches?productModelId={product['id']}"
    ).get_json()["data"][0]
    assert batch["quantity"] == 1000


def test_part_codes_are_generated_in_a_batch_and_each_code_accepts_entry_data(client):
    supplier, part = create_supplier_and_part(client, "BATCH")
    labels = post_json(
        client,
        "/api/part-labels",
        {
            "partTypeId": part["id"],
            "lotNo": "LOT-BATCH-01",
            "supplierBatchNo": "SUP-LOT-99",
            "productionDate": "2026-07-16",
            "generatedBy": "OP-BATCH",
            "quantity": 3,
        },
    )
    assert len(labels) == 3
    assert len({label["identificationCode"] for label in labels}) == 3
    assert len({label["batchCode"] for label in labels}) == 1
    assert all(label["batchQuantity"] == 3 for label in labels)
    assert all(label["batchGeneratedAt"] for label in labels)
    assert all(label["supplierCode"] == supplier["supplierCode"] for label in labels)

    batches = client.get("/api/part-label-batches").get_json()["data"]
    assert len(batches) == 1
    assert batches[0]["batchCode"] == labels[0]["batchCode"]
    assert batches[0]["quantity"] == 3
    assert batches[0]["generatedCount"] == 3
    assert batches[0]["dataEnteredCount"] == 0
    assert batches[0]["generatedBy"] == "OP-BATCH"
    assert batches[0]["generatedAt"]

    response = client.put(
        f"/api/part-labels/{labels[0]['id']}",
        json={
            "sourceSerialNo": "VENDOR-SN-0001",
            "productionDate": "2026-07-15",
            "inspectionStatus": "PASS",
            "enteredBy": "QC-01",
            "remarks": "外观及通电检验合格",
        },
    )
    assert response.status_code == 200
    entered = response.get_json()["data"]
    assert entered["dataEntered"] is True
    assert entered["sourceSerialNo"] == "VENDOR-SN-0001"
    assert entered["inspectionStatus"] == "PASS"
    assert entered["enteredBy"] == "QC-01"
    assert entered["enteredAt"]
    assert entered["supplierName"] == supplier["name"]

    duplicate = client.put(
        f"/api/part-labels/{labels[1]['id']}",
        json={"sourceSerialNo": "VENDOR-SN-0001", "enteredBy": "QC-02"},
    )
    assert duplicate.status_code == 409
    batches = client.get("/api/part-label-batches").get_json()["data"]
    assert batches[0]["dataEnteredCount"] == 1

    detail = client.get(f"/api/part-label-batches/{batches[0]['id']}")
    assert detail.status_code == 200
    detail_data = detail.get_json()["data"]
    assert detail_data["batch"]["batchCode"] == labels[0]["batchCode"]
    assert len(detail_data["labels"]) == 3

    download = client.get(f"/api/part-label-batches/{batches[0]['id']}/qrcodes.zip")
    assert download.status_code == 200
    assert download.mimetype == "application/zip"
    with ZipFile(BytesIO(download.data)) as archive:
        names = set(archive.namelist())
        assert {"QR_0001.svg", "QR_0002.svg", "QR_0003.svg", "编码清单.csv"}.issubset(names)
        manifest = archive.read("编码清单.csv").decode("utf-8-sig")
    assert labels[0]["identificationCode"] in manifest


def test_part_code_generation_retries_a_database_collision(client, monkeypatch):
    _, part = create_supplier_and_part(client, "CODE-RETRY")
    existing = create_labels(client, part["id"], 1)[0]["identificationCode"]
    generated = iter([existing, "PTS:P:UNIQUECODE00000000000001"])
    monkeypatch.setattr("app.new_part_identification_code", lambda: next(generated))

    retried = create_labels(client, part["id"], 1)[0]
    assert retried["identificationCode"] == "PTS:P:UNIQUECODE00000000000001"
    assert retried["identificationCode"] != existing


def test_interleaved_workstations_do_not_mix_scan_sessions(client):
    _, part_a = create_supplier_and_part(client, "A")
    _, part_b = create_supplier_and_part(client, "B")
    labels_a = create_labels(client, part_a["id"], 2)
    labels_b = create_labels(client, part_b["id"], 2)
    machine_a = create_machine(client, "TW-04-000000000000000000000001")
    machine_b = create_machine(client, "TW-04-000000000000000000000002")

    assert scan(client, "STATION-A-001", machine_a["identificationCode"])["data"]["completed"] is False
    assert scan(client, "STATION-B-001", machine_b["identificationCode"])["data"]["completed"] is False
    scan(client, "STATION-A-001", labels_a[0]["identificationCode"])
    scan(client, "STATION-B-001", labels_b[0]["identificationCode"])
    result_a = scan(client, "STATION-A-001", labels_a[1]["identificationCode"])["data"]
    result_b = scan(client, "STATION-B-001", labels_b[1]["identificationCode"])["data"]

    assert result_a["completed"] is True
    assert result_b["completed"] is True
    assert result_a["record"]["machine"]["sn"] == machine_a["sn"]
    assert {part["identificationCode"] for part in result_a["record"]["parts"]} == {
        label["identificationCode"] for label in labels_a
    }
    assert result_b["record"]["machine"]["sn"] == machine_b["sn"]


def test_machine_and_part_are_reserved_across_workstations(client):
    _, part = create_supplier_and_part(client, "RESERVE")
    labels = create_labels(client, part["id"], 2)
    machine = create_machine(client, "TW-04-000000000000000000000003")

    scan(client, "STATION-A-002", machine["identificationCode"])
    second_machine_scan = scan(client, "STATION-B-002", machine["identificationCode"], expected=409)
    assert "工位" in second_machine_scan["message"]

    scan(client, "STATION-A-002", labels[0]["identificationCode"])
    other_machine = create_machine(client, "TW-04-000000000000000000000004")
    scan(client, "STATION-B-003", other_machine["identificationCode"])
    reserved_part = scan(client, "STATION-B-003", labels[0]["identificationCode"], expected=409)
    assert "工位" in reserved_part["message"]


def test_quality_release_gate_is_opt_in_and_blocks_non_pass_parts(client):
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.get_json()["data"]["requireQualityRelease"] is False

    _, part = create_supplier_and_part(client, "QUALITY-GATE")
    labels = create_labels(client, part["id"], 2)
    machine = create_machine(client, "TW-04-000000000000000000000050")

    enabled = client.put(
        "/api/settings",
        json={"requireQualityRelease": True},
    )
    assert enabled.status_code == 200, enabled.get_json()
    assert enabled.get_json()["data"]["requireQualityRelease"] is True

    scan(client, "STATION-QUALITY", machine["identificationCode"])
    pending = scan(
        client,
        "STATION-QUALITY",
        labels[0]["identificationCode"],
        expected=409,
    )
    assert "待检验" in pending["message"]

    passed = client.put(
        f"/api/part-labels/{labels[0]['id']}",
        json={"inspectionStatus": "PASS", "enteredBy": "QC-QUALITY"},
    )
    assert passed.status_code == 200, passed.get_json()
    assert scan(
        client, "STATION-QUALITY", labels[0]["identificationCode"]
    )["data"]["completed"] is False

    failed = client.put(
        f"/api/part-labels/{labels[1]['id']}",
        json={"inspectionStatus": "FAIL", "enteredBy": "QC-QUALITY"},
    )
    assert failed.status_code == 200, failed.get_json()
    rejected = scan(
        client,
        "STATION-QUALITY",
        labels[1]["identificationCode"],
        expected=409,
    )
    assert "不合格" in rejected["message"]

    repassed = client.put(
        f"/api/part-labels/{labels[1]['id']}",
        json={"inspectionStatus": "PASS", "enteredBy": "QC-QUALITY"},
    )
    assert repassed.status_code == 200, repassed.get_json()
    completed = scan(
        client, "STATION-QUALITY", labels[1]["identificationCode"]
    )["data"]
    assert completed["completed"] is True


def test_last_component_scan_can_be_undone_with_reason_and_audit(client):
    _, part = create_supplier_and_part(client, "UNDO")
    label = create_labels(client, part["id"], 1)[0]
    first_machine = create_machine(client, "TW-04-000000000000000000000051")

    scan(client, "STATION-UNDO-A", first_machine["identificationCode"])
    scan(client, "STATION-UNDO-A", label["identificationCode"])

    missing_reason = client.post(
        "/api/scan/undo",
        json={"stationId": "STATION-UNDO-A", "reason": ""},
    )
    assert missing_reason.status_code == 400

    undone = client.post(
        "/api/scan/undo",
        json={"stationId": "STATION-UNDO-A", "reason": "操作员发现扫错工位"},
    )
    assert undone.status_code == 200, undone.get_json()
    undone_data = undone.get_json()["data"]
    assert undone_data["undone"]["identificationCode"] == label["identificationCode"]
    assert undone_data["session"]["parts"] == []

    genealogy = client.get(
        f"/api/genealogy?code={first_machine['identificationCode']}"
    )
    assert genealogy.status_code == 200, genealogy.get_json()
    undo_events = [
        event
        for event in genealogy.get_json()["data"]["events"]
        if event["eventType"] == "COMPONENT_SCAN_UNDONE"
    ]
    assert len(undo_events) == 1
    assert undo_events[0]["reason"] == "操作员发现扫错工位"
    assert undo_events[0]["stationId"] == "STATION-UNDO-A"

    second_machine = create_machine(client, "TW-04-000000000000000000000052")
    scan(client, "STATION-UNDO-B", second_machine["identificationCode"])
    rescanned = scan(client, "STATION-UNDO-B", label["identificationCode"])
    assert rescanned["data"]["completed"] is False


def test_two_final_writes_can_run_concurrently(app):
    with app.test_client() as setup_client:
        _, part = create_supplier_and_part(setup_client, "CONCURRENT")
        labels = create_labels(setup_client, part["id"], 4)
        machines = [
            create_machine(setup_client, "TW-04-000000000000000000000006"),
            create_machine(setup_client, "TW-04-000000000000000000000007"),
        ]
        for index in range(2):
            station = f"STATION-CONCURRENT-{index}"
            scan(setup_client, station, machines[index]["identificationCode"])
            scan(setup_client, station, labels[index * 2]["identificationCode"])

    def finish(index: int):
        with app.test_client() as worker:
            return scan(
                worker,
                f"STATION-CONCURRENT-{index}",
                labels[index * 2 + 1]["identificationCode"],
            )["data"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, [0, 1]))
    assert all(result["completed"] for result in results)
    with app.test_client() as verify_client:
        records = verify_client.get("/api/records").get_json()["data"]
    assert len(records) == 2


def test_excel_export_is_a_valid_dynamic_xlsx(client):
    _, part = create_supplier_and_part(client, "EXPORT")
    labels = create_labels(client, part["id"], 2)
    client.put(
        f"/api/part-labels/{labels[0]['id']}",
        json={
            "sourceSerialNo": "EXPORT-VENDOR-SN-01",
            "inspectionStatus": "PASS",
            "enteredBy": "EXPORT-QC",
        },
    )
    machine = create_machine(client, "TW-04-000000000000000000000008")
    scan(client, "STATION-EXPORT", machine["identificationCode"])
    for label in labels:
        scan(client, "STATION-EXPORT", label["identificationCode"])

    response = client.get("/api/records/export.xlsx")
    assert response.status_code == 200
    assert response.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    with ZipFile(BytesIO(response.data)) as archive:
        names = set(archive.namelist())
        assert "xl/worksheets/sheet1.xml" in names
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "产品分类" in sheet
        assert "成品 SN" in sheet
        assert "部件2识别码" in sheet
        assert "部件1分类编码" in sheet
        assert "部件1编码批次" in sheet
        assert "部件1供应商单件序列号" in sheet
        assert "部件1单码录入时间" in sheet
        assert "EXPORT-VENDOR-SN-01" in sheet
        assert machine["sn"] in sheet
        core_properties = archive.read("docProps/core.xml").decode("utf-8")
        app_properties = archive.read("docProps/app.xml").decode("utf-8")
        assert "聚星同创仓库管理系统" in core_properties
        assert "聚星同创仓库管理系统" in app_properties
        assert "PaceFit" not in core_properties
        assert "PaceFit" not in app_properties


def test_telemetry_identity_parser_uses_full_manufacturer_sn():
    identity = _extract_identity(
        {
            "type": "telemetry",
            "data": {
                "manufacturerData": {
                    "model": "TW-04",
                    "sn": "TW-04-0123456789ABCDEF01234567",
                    "productionDate": "20260716",
                }
            },
        }
    )
    assert identity == ("TW-04-0123456789ABCDEF01234567", "TW-04", "20260716")
    assert _extract_identity({"type": "heartbeat", "data": {}}) is None


def test_frontend_is_served_without_external_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "聚星同创仓库管理系统" in html
    assert "PaceFit" not in html
    assert 'id="login-form"' in html
    assert 'id="product-form"' in html
    assert 'id="user-form"' in html
    assert 'data-role="ADMIN"' in html
    assert 'data-role="WAREHOUSE"' in html
    assert 'data-role="OPERATIONS"' in html
    assert 'id="genealogy-modal"' in html
    assert 'id="code-modal"' in html
    assert 'id="record-edit-modal"' in html
    assert 'id="record-delete-modal"' in html
    assert 'id="record-quality-modal"' in html
    assert 'id="inventory-movement-modal"' in html
    assert 'id="dashboard-quality-queue"' in html
    assert 'id="dashboard-quality-bulk-button"' in html
    assert 'id="trace-record-table"' in html
    assert 'id="trace-status-filter"' in html
    assert 'id="trace-date-from"' in html
    assert 'id="trace-date-to"' in html
    assert 'id="dashboard-stock-alerts"' in html
    assert 'data-view="batch-entry"' in html
    assert 'data-view="my-records"' in html
    assert 'data-role="ADMIN,WAREHOUSE"' in html
    assert 'data-view="users"' in html
    assert 'id="supplier-product-usage-table"' in html
    assert 'name="role"' in html
    assert "当前工位" not in html
    assert "用户管理" in html
    assert "添加新产品" in html
    assert "产品录入记录" in html
    assert "http://" not in html
    assert "https://" not in html
    script = client.get("/static/app_v2.js")
    assert script.status_code == 200
    javascript = script.get_data(as_text=True)
    assert "handleBatchEntryKeydown" in javascript
    assert ".requestSubmit()" in javascript
    assert "requestAnimationFrame(apply)" in javascript
    assert "setTimeout(apply, 180)" in javascript
    assert 'if (isWarehouse()) return ["batch-gen", "batch-entry", "inbound-receipts", "production-orders", "scan-gun", "batch-trace", "my-records"]' in javascript
    assert 'if (isOperations()) return ["products", "purchase-orders", "batch-trace", "inventory-sync"]' in javascript
    assert 'data-view="suppliers"' in html
    assert 'id="supplier-grid"' in html
    assert 'id="product-batch-detail"' in html
    assert 'id="delete-product"' in html
    assert "handleRecordScannerKeydown" in javascript
    assert "openInventoryMovements" in javascript
    assert "submitRecordQuality" in javascript
    assert "openBulkQuality" in javascript
    assert '"/api/records/status/bulk"' in javascript
    assert "genealogy-main-qr" in javascript
    assert "dispatchDataAction" in javascript
    assert '"open-supplier":' in javascript
    assert '"open-related-product":' in javascript
    assert "record-scan-input" in javascript
    stylesheet = client.get("/static/styles_v2.css")
    assert stylesheet.status_code == 200
    css = stylesheet.get_data(as_text=True)
    assert ".trace-filter-grid" in css
    assert ".genealogy-part img" in css
    assert "linear-gradient" not in css
    html_ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(html_ids) == len(set(html_ids))
    referenced_ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"', javascript))
    assert referenced_ids.issubset(set(html_ids))
    rendered_actions = set(re.findall(r'data-action="([a-z0-9-]+)"', javascript))
    registered_actions = set(re.findall(r'^\s+"([a-z0-9-]+)": \{ run:', javascript, re.MULTILINE))
    assert rendered_actions.issubset(registered_actions)
    for button_tag in re.findall(r"<button\b[^>]*>", html):
        if any(attribute in button_tag for attribute in ("data-view=", "data-go=", "data-close=", "data-action=")):
            continue
        if 'type="submit"' in button_tag:
            continue
        button_id = re.search(r'id="([^"]+)"', button_tag)
        assert button_id, f"没有操作标识的按钮: {button_tag}"
        assert f'$("#{button_id.group(1)}")' in javascript, f"按钮未绑定事件: {button_id.group(1)}"


def test_missing_routes_keep_their_http_status_without_internal_error(client):
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 204

    missing_api = client.get("/api/route-that-does-not-exist")
    assert missing_api.status_code == 404
    assert missing_api.get_json() == {"ok": False, "message": "接口不存在"}

    missing_page = client.get("/page-that-does-not-exist")
    assert missing_page.status_code == 404
    assert "系统处理失败" not in missing_page.get_data(as_text=True)


def test_windows_launch_scripts_use_cmd_safe_encoding():
    project_root = Path(__file__).resolve().parents[1]
    for filename in ("start.bat", "install.bat", "backup.bat", "settings.example.bat"):
        content = (project_root / filename).read_bytes()
        assert content.startswith(b"\xef\xbb\xbf"), f"{filename} 必须使用 UTF-8 BOM"
        body = content[3:]
        assert b"\n" not in body.replace(b"\r\n", b""), f"{filename} 必须使用 CRLF 换行"
        body.decode("utf-8")


def test_bom_slots_reject_wrong_part_type_and_snapshot_version(client):
    _, motor = create_supplier_and_part(client, "MOTOR")
    _, controller = create_supplier_and_part(client, "CTRL")
    plan_v1 = create_trace_plan(
        client,
        "V1.0",
        [
            {"slotName": "驱动电机", "partTypeId": motor["id"], "quantity": 1},
            {"slotName": "主控板", "partTypeId": controller["id"], "quantity": 1},
        ],
    )
    old_machine = create_machine(client, "TW-04-100000000000000000000001")
    assert old_machine["tracePlanId"] == plan_v1["id"]

    plan_v2 = create_trace_plan(
        client,
        "V2.0",
        [
            {"slotName": "主控板", "partTypeId": controller["id"], "quantity": 1},
            {"slotName": "驱动电机", "partTypeId": motor["id"], "quantity": 1},
        ],
    )
    new_machine = create_machine(client, "TW-04-100000000000000000000002")
    assert new_machine["tracePlanId"] == plan_v2["id"]
    # V2 activation does not rewrite an already commissioned machine's V1 snapshot.
    listed = client.get("/api/machines").get_json()["data"]
    old_listed = next(item for item in listed if item["id"] == old_machine["id"])
    assert old_listed["tracePlanId"] == plan_v1["id"]

    motor_labels = create_labels(client, motor["id"], 2)
    controller_label = create_labels(client, controller["id"], 1)[0]
    started = scan(client, "STATION-BOM-001", old_machine["identificationCode"])["data"]
    assert started["session"]["workflowMode"] == "BOM"
    assert [slot["partCode"] for slot in started["session"]["expectedSlots"]] == [
        motor["partCode"], controller["partCode"]
    ]
    scan(client, "STATION-BOM-001", motor_labels[0]["identificationCode"])
    wrong = scan(client, "STATION-BOM-001", motor_labels[1]["identificationCode"], expected=409)
    assert "BOM" in wrong["message"]
    assert controller["partCode"] in wrong["message"]
    final = scan(client, "STATION-BOM-001", controller_label["identificationCode"])["data"]
    assert final["record"]["tracePlan"]["version"] == "V1.0"


def test_same_component_category_accepts_parts_from_different_suppliers(client):
    supplier_a, _ = create_supplier_and_part(client, "CAT-A")
    supplier_b, _ = create_supplier_and_part(client, "CAT-B")
    motor_a = post_json(
        client,
        "/api/part-types",
        {
            "categoryCode": "MOTOR",
            "categoryName": "驱动电机",
            "partCode": "MOTOR-A-SKU",
            "name": "A 厂电机",
            "supplierId": supplier_a["id"],
        },
    )
    motor_b = post_json(
        client,
        "/api/part-types",
        {
            "categoryCode": "MOTOR",
            "categoryName": "驱动电机",
            "partCode": "MOTOR-B-SKU",
            "name": "B 厂电机",
            "supplierId": supplier_b["id"],
        },
    )
    assert motor_a["categoryCode"] == motor_b["categoryCode"] == "MOTOR"
    create_trace_plan(
        client,
        "V-CATEGORY",
        [{"slotName": "驱动电机", "partTypeId": motor_a["id"], "quantity": 1}],
    )
    machine = create_machine(client, "TW-04-CATEGORY000000000000001")
    label = create_labels(client, motor_b["id"], 1)[0]
    scan(client, "STATION-CATEGORY", machine["identificationCode"])
    completed = scan(client, "STATION-CATEGORY", label["identificationCode"])["data"]
    assert completed["completed"] is True
    assert completed["record"]["parts"][0]["supplierCode"] == supplier_b["supplierCode"]


def test_genealogy_supports_backward_machine_and_forward_component_trace(client):
    _, part = create_supplier_and_part(client, "GENEALOGY")
    create_trace_plan(
        client,
        "V1",
        [{"slotName": "核心部件", "partTypeId": part["id"], "quantity": 1}],
    )
    label = create_labels(client, part["id"], 1)[0]
    machine = create_machine(client, "TW-04-200000000000000000000001")
    scan(client, "STATION-GENEALOGY", machine["identificationCode"])
    scan(client, "STATION-GENEALOGY", label["identificationCode"])

    backward_response = client.get(
        f"/api/genealogy?code={machine['identificationCode']}"
    )
    assert backward_response.status_code == 200
    backward = backward_response.get_json()["data"]
    assert backward["queryType"] == "MACHINE"
    assert backward["direction"] == "BACKWARD"
    assert backward["machine"]["productFamilyCode"] == "LEGACY_UNIT"
    assert backward["machine"]["productModelName"] == "通用逐台产品"
    assert backward["records"][0]["machine"]["productFamilyCode"] == "LEGACY_UNIT"
    assert backward["records"][0]["parts"][0]["identificationCode"] == label["identificationCode"]
    event_types = [event["eventType"] for event in backward["events"]]
    assert "MACHINE_COMMISSIONED" in event_types
    assert "ASSEMBLY_STARTED" in event_types
    assert "COMPONENT_SCANNED" in event_types
    assert "ASSEMBLY_COMPLETED" in event_types

    forward_response = client.get(
        f"/api/genealogy?code={label['identificationCode']}"
    )
    assert forward_response.status_code == 200
    forward = forward_response.get_json()["data"]
    assert forward["queryType"] == "PART_LABEL"
    assert forward["direction"] == "FORWARD"
    assert forward["records"][0]["machine"]["sn"] == machine["sn"]


def test_v1_database_is_migrated_without_dropping_existing_rows(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE machines (
          id INTEGER PRIMARY KEY AUTOINCREMENT, sn TEXT NOT NULL UNIQUE,
          model TEXT NOT NULL, production_date TEXT NOT NULL DEFAULT '',
          identification_code TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE suppliers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_code TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE part_types (
          id INTEGER PRIMARY KEY AUTOINCREMENT, part_code TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL, specification TEXT NOT NULL DEFAULT '', supplier_id INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE part_labels (
          id INTEGER PRIMARY KEY AUTOINCREMENT, identification_code TEXT NOT NULL UNIQUE,
          part_type_id INTEGER NOT NULL, lot_no TEXT NOT NULL DEFAULT '',
          supplier_batch_no TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE scan_sessions (
          station_id TEXT PRIMARY KEY, station_name TEXT NOT NULL DEFAULT '',
          operator_name TEXT NOT NULL DEFAULT '', machine_id INTEGER, updated_at TEXT NOT NULL
        );
        CREATE TABLE trace_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, trace_no TEXT NOT NULL UNIQUE,
          machine_id INTEGER NOT NULL UNIQUE, station_id TEXT NOT NULL,
          station_name TEXT NOT NULL DEFAULT '', operator_name TEXT NOT NULL DEFAULT '',
          completed_at TEXT NOT NULL
        );
        INSERT INTO machines(sn, model, identification_code, created_at)
        VALUES ('TW-04-LEGACY000000000000000001', 'TW-04',
                'PTS:M:TW-04-LEGACY000000000000000001', '2026-07-16T00:00:00+08:00');
        INSERT INTO suppliers(supplier_code, name, created_at)
        VALUES ('SUP-LEGACY', '旧供应商', '2026-07-16T00:00:00+08:00');
        INSERT INTO part_types(part_code, name, supplier_id, created_at)
        VALUES ('PART-LEGACY', '旧部件', 1, '2026-07-16T00:00:00+08:00');
        INSERT INTO part_labels(identification_code, part_type_id, lot_no, created_at)
        VALUES ('PTS:P:LEGACY000000000000000001', 1, 'LOT-OLD', '2026-07-16T00:00:00+08:00');
        """
    )
    connection.commit()
    connection.close()

    migrated_app = create_app({"TESTING": True, "DATABASE": str(database_path)})
    with migrated_app.app_context():
        migrated = sqlite3.connect(database_path)
        migrated.row_factory = sqlite3.Row
        try:
            machine_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(machines)")}
            record_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(trace_records)")}
            label_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(part_labels)")}
            part_type_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(part_types)")}
            user_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(users)")}
            assert "trace_plan_id" in machine_columns
            assert {
                "trace_plan_id", "status", "remarks", "status_reason",
                "status_updated_at", "status_updated_by_user_id",
            }.issubset(record_columns)
            assert {
                "label_batch_id", "source_serial_no", "production_date", "inspection_status",
                "remarks", "entered_by", "entered_at", "updated_at",
            }.issubset(label_columns)
            assert {"category_code", "category_name", "minimum_stock"}.issubset(part_type_columns)
            assert migrated.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'part_label_batches'"
            ).fetchone()["n"] == 1
            assert migrated.execute("SELECT COUNT(*) AS n FROM machines").fetchone()["n"] == 1
            legacy_label = migrated.execute(
                "SELECT identification_code, lot_no, source_serial_no, entered_at FROM part_labels"
            ).fetchone()
            assert legacy_label["identification_code"] == "PTS:P:LEGACY000000000000000001"
            assert legacy_label["lot_no"] == "LOT-OLD"
            assert legacy_label["source_serial_no"] == ""
            assert legacy_label["entered_at"] is None
            legacy_part = migrated.execute(
                "SELECT part_code, name, category_code, category_name FROM part_types"
            ).fetchone()
            assert legacy_part["category_code"] == legacy_part["part_code"]
            assert legacy_part["category_name"] == legacy_part["name"]
            assert {"product_model_id", "trace_plan_id"}.issubset(machine_columns)
            assert {"completed_by_user_id"}.issubset(record_columns)
            assert {"entered_by_user_id"}.issubset(label_columns)
            assert migrated.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()["n"] == 1
            assert "session_version" in user_columns
            assert migrated.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'product_families'"
            ).fetchone()["n"] == 1
            migrated_machine = migrated.execute(
                """
                SELECT m.sn, pm.model_code, pm.bluetooth_notify_uuid
                FROM machines m JOIN product_models pm ON pm.id = m.product_model_id
                """
            ).fetchone()
            assert migrated_machine["sn"] == "TW-04-LEGACY000000000000000001"
            assert migrated_machine["model_code"] == "TW-04"
            assert migrated_machine["bluetooth_notify_uuid"] == "0000fff1-0000-1000-8000-00805f9b34fb"
            assert migrated.execute("PRAGMA user_version").fetchone()[0] == 22
            # v16: the extended product profile column exists, and a production
            # batch may omit its trace plan (component-less finished goods).
            product_model_columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(product_models)")
            }
            assert "attributes_json" in product_model_columns
            trace_plan_notnull = {
                row["name"]: row["notnull"]
                for row in migrated.execute("PRAGMA table_info(production_batches)")
            }
            assert trace_plan_notnull["trace_plan_id"] == 0
            assert trace_plan_notnull["product_model_id"] == 1
            assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
            slot_columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(trace_plan_slots)")
            }
            code_set_columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(product_code_sets)")
            }
            assert "supplier_inventory_batch_id" in slot_columns
            assert "generation_batch_id" in code_set_columns
            assert migrated.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type = 'table' AND name = 'supplier_inventory_batches'"
            ).fetchone()["n"] == 1
            assert migrated.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type = 'table' AND name = 'product_code_batches'"
            ).fetchone()["n"] == 1
        finally:
            migrated.close()


@pytest.fixture()
def auth_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "AUTH_DISABLED": False,
            "SECRET_KEY": "traceability-auth-test-secret",
            "DATABASE": str(tmp_path / "traceability-auth.db"),
            "BOOTSTRAP_ADMIN_USERNAME": "admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "Admin@12345",
            "BOOTSTRAP_ADMIN_DISPLAY_NAME": "测试管理员",
        }
    )


def login_and_change_password(client, username: str, password: str, new_password: str):
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.get_json()
    user = login.get_json()["data"]
    csrf = user["csrfToken"]
    if user["mustChangePassword"]:
        changed = client.post(
            "/api/auth/change-password",
            json={"currentPassword": password, "newPassword": new_password},
            headers={"X-CSRF-Token": csrf},
        )
        assert changed.status_code == 200, changed.get_json()
        user = changed.get_json()["data"]
        csrf = user["csrfToken"]
    return user, csrf


def test_auth_roles_products_and_operator_identity_are_enforced(auth_app):
    anonymous = auth_app.test_client()
    assert anonymous.get("/api/product-models").status_code == 401

    admin = auth_app.test_client()
    first_login = admin.post(
        "/api/auth/login", json={"username": "admin", "password": "Admin@12345"}
    )
    assert first_login.status_code == 200
    first_data = first_login.get_json()["data"]
    assert first_data["mustChangePassword"] is True
    assert admin.get("/api/product-models").status_code == 428
    assert admin.post(
        "/api/auth/change-password",
        json={"currentPassword": "Admin@12345", "newPassword": "Admin@98765"},
    ).status_code == 403
    changed = admin.post(
        "/api/auth/change-password",
        json={"currentPassword": "Admin@12345", "newPassword": "Admin@98765"},
        headers={"X-CSRF-Token": first_data["csrfToken"]},
    )
    assert changed.status_code == 200
    admin_csrf = changed.get_json()["data"]["csrfToken"]

    second_admin_response = admin.post(
        "/api/users",
        json={
            "username": "quality.admin",
            "displayName": "质量管理员",
            "password": "Admin@24680",
            "role": "ADMIN",
            "productModelIds": [1],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert second_admin_response.status_code == 201, second_admin_response.get_json()
    second_admin = second_admin_response.get_json()["data"]
    assert second_admin["role"] == "ADMIN"
    assert second_admin["productModelIds"] == []

    family_response = admin.post(
        "/api/product-families",
        json={"productCode": "BED_FRAME", "name": "电动床架", "description": "后续产品线"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert family_response.status_code == 201, family_response.get_json()
    family = family_response.get_json()["data"]
    model_response = admin.post(
        "/api/product-models",
        json={
            "productFamilyId": family["id"],
            "modelCode": "BF-01",
            "name": "BF-01 电动床架",
            "serialPrefix": "BF-01",
            "identitySource": "SCANNER",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert model_response.status_code == 201, model_response.get_json()
    product_model = model_response.get_json()["data"]
    assert product_model["productFamilyCode"] == "BED_FRAME"
    assert product_model["identitySource"] == "SCANNER"

    supplier_response = admin.post(
        "/api/suppliers",
        json={"supplierCode": "SUP-AUTH", "name": "权限测试供应商"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert supplier_response.status_code == 201
    supplier = supplier_response.get_json()["data"]
    part_response = admin.post(
        "/api/part-types",
        json={
            "partCode": "MOTOR-AUTH",
            "name": "测试电机",
            "categoryCode": "MOTOR",
            "categoryName": "电机",
            "supplierId": supplier["id"],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert part_response.status_code == 201
    part = part_response.get_json()["data"]

    blocked_supplier_response = admin.post(
        "/api/suppliers",
        json={"supplierCode": "SUP-BLOCKED", "name": "未授权供应商"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert blocked_supplier_response.status_code == 201
    blocked_supplier = blocked_supplier_response.get_json()["data"]
    blocked_part_response = admin.post(
        "/api/part-types",
        json={
            "partCode": "MOTOR-BLOCKED",
            "name": "未授权电机",
            "categoryCode": "MOTOR-BLOCKED",
            "categoryName": "未授权电机",
            "supplierId": blocked_supplier["id"],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert blocked_part_response.status_code == 201
    blocked_part = blocked_part_response.get_json()["data"]
    blocked_label_response = admin.post(
        "/api/part-labels",
        json={"partTypeId": blocked_part["id"], "quantity": 1},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert blocked_label_response.status_code == 201
    blocked_label = blocked_label_response.get_json()["data"][0]
    treadmill_model = next(
        item
        for item in admin.get("/api/product-models").get_json()["data"]
        if item["modelCode"] == "TW-04"
    )
    blocked_model_response = admin.post(
        "/api/product-models",
        json={
            "productFamilyId": family["id"],
            "modelCode": "BF-02",
            "name": "BF-02 未授权床架",
            "serialPrefix": "BF-02",
            "identitySource": "SCANNER",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert blocked_model_response.status_code == 201, blocked_model_response.get_json()
    blocked_model = blocked_model_response.get_json()["data"]
    blocked_machine_response = admin.post(
        "/api/machines",
        json={
            "sn": "BF-02-UNAUTHORIZED-000002",
            "productModelId": blocked_model["id"],
            "productionDate": "20260717",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert blocked_machine_response.status_code == 201
    blocked_machine = blocked_machine_response.get_json()["data"]

    operator_response = admin.post(
        "/api/users",
        json={
            "username": "operator.one",
            "displayName": "录入员一号",
            "password": "Operator@123",
            "role": "WAREHOUSE",
            "productModelIds": [product_model["id"]],
            "supplierIds": [supplier["id"]],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert operator_response.status_code == 201, operator_response.get_json()

    operator = auth_app.test_client()
    operator_user, operator_csrf = login_and_change_password(
        operator, "operator.one", "Operator@123", "Operator@456"
    )
    assert operator_user["role"] == "WAREHOUSE"
    assert operator_user["displayName"] == "录入员一号"
    assert operator_user["productModelIds"] == [product_model["id"]]
    assert operator_user["supplierIds"] == [supplier["id"]]
    # Product scoping removed: a warehouse user now sees every product model and
    # supplier, exactly like an administrator.
    operator_model_ids = {item["id"] for item in operator.get("/api/product-models").get_json()["data"]}
    admin_model_ids = {item["id"] for item in admin.get("/api/product-models").get_json()["data"]}
    assert operator_model_ids == admin_model_ids
    assert product_model["id"] in operator_model_ids
    operator_supplier_ids = {item["id"] for item in operator.get("/api/suppliers").get_json()["data"]}
    admin_supplier_ids = {item["id"] for item in admin.get("/api/suppliers").get_json()["data"]}
    assert operator_supplier_ids == admin_supplier_ids
    forbidden = operator.post(
        "/api/suppliers",
        json={"supplierCode": "NOPE", "name": "不应创建"},
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert forbidden.status_code == 403

    forbidden_model = operator.post(
        "/api/machines",
        json={
            "sn": "TW-04-UNAUTHORIZED-000001",
            "productModelId": treadmill_model["id"],
            "productionDate": "20260717",
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert forbidden_model.status_code == 403
    assert "仅管理员" in forbidden_model.get_json()["message"]

    forbidden_supplier = operator.post(
        "/api/part-labels",
        json={"partTypeId": blocked_part["id"], "quantity": 1},
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert forbidden_supplier.status_code == 403
    assert "仅管理员" in forbidden_supplier.get_json()["message"]

    # Scope removed: a warehouse user may now scan a machine of any product
    # model (this product was previously un-granted and blocked).
    unscoped_machine_scan = operator.post(
        "/api/scan",
        json={
            "stationId": "STATION-SCOPE-BLOCKED",
            "stationName": "任意产品工位",
            "code": blocked_machine["identificationCode"],
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert unscoped_machine_scan.status_code == 200, unscoped_machine_scan.get_json()

    wrong_product = operator.post(
        "/api/machines",
        json={
            "sn": "TW-04-00000001",
            "productModelId": product_model["id"],
            "productionDate": "20260717",
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert wrong_product.status_code == 403

    machine_response = admin.post(
        "/api/machines",
        json={
            "sn": "BF-01-00000001",
            "productModelId": product_model["id"],
            "productionDate": "20260717",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert machine_response.status_code == 201, machine_response.get_json()
    machine = machine_response.get_json()["data"]
    assert machine["productFamilyCode"] == "BED_FRAME"
    assert machine["productModelName"] == "BF-01 电动床架"

    scan_started = operator.post(
        "/api/scan",
        json={
            "stationId": "STATION-SCOPE-ALLOWED",
            "stationName": "授权工位",
            "code": machine["identificationCode"],
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert scan_started.status_code == 200, scan_started.get_json()
    # Scope removed: a warehouse user may now scan a part from any supplier.
    unscoped_part_scan = operator.post(
        "/api/scan",
        json={
            "stationId": "STATION-SCOPE-ALLOWED",
            "stationName": "授权工位",
            "code": blocked_label["identificationCode"],
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert unscoped_part_scan.status_code == 200, unscoped_part_scan.get_json()

    forbidden_label_generation = operator.post(
        "/api/part-labels",
        json={"partTypeId": part["id"], "quantity": 1, "generatedBy": "伪造生成者"},
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert forbidden_label_generation.status_code == 403
    labels_response = admin.post(
        "/api/part-labels",
        json={"partTypeId": part["id"], "quantity": 1, "generatedBy": "伪造生成者"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert labels_response.status_code == 201, labels_response.get_json()
    label = labels_response.get_json()["data"][0]
    assert label["batchGeneratedBy"] == "测试管理员"
    entered = operator.put(
        f"/api/part-labels/{label['id']}",
        json={"sourceSerialNo": "VENDOR-AUTH-001", "enteredBy": "伪造录入者"},
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert entered.status_code == 403

    reset = admin.put(
        f"/api/users/{operator_user['id']}",
        json={"password": "Operator@789"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert reset.status_code == 200, reset.get_json()
    assert reset.get_json()["data"]["mustChangePassword"] is True
    assert reset.get_json()["data"]["productModelIds"] == [product_model["id"]]
    assert reset.get_json()["data"]["supplierIds"] == [supplier["id"]]
    assert operator.get("/api/product-models").status_code == 401
    assert operator.post(
        "/api/auth/login", json={"username": "operator.one", "password": "Operator@456"}
    ).status_code == 401
    relogin = operator.post(
        "/api/auth/login", json={"username": "operator.one", "password": "Operator@789"}
    )
    assert relogin.status_code == 200
    assert relogin.get_json()["data"]["mustChangePassword"] is True


def test_simplified_operator_scope_is_product_only_and_derives_component_suppliers(auth_app):
    admin = auth_app.test_client()
    _, admin_csrf = login_and_change_password(
        admin, "admin", "Admin@12345", "Admin@98765"
    )
    product_response = admin.post(
        "/api/products",
        json={
            "name": "授权产品 A",
            "components": [
                {"name": "控制板", "supplierName": "授权供应商 A", "quantity": 1}
            ],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert product_response.status_code == 201, product_response.get_json()
    product = product_response.get_json()["data"]
    other_response = admin.post(
        "/api/products",
        json={
            "name": "未授权产品 B",
            "components": [
                {"name": "线束", "supplierName": "未授权供应商 B", "quantity": 1}
            ],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert other_response.status_code == 201

    created = admin.post(
        "/api/users",
        json={
            "username": "simple.operator",
            "displayName": "简版录入员",
            "password": "Operator@123",
            "role": "WAREHOUSE",
            "productModelIds": [product["id"]],
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201, created.get_json()
    created_user = created.get_json()["data"]
    assert created_user["productModelIds"] == [product["id"]]
    assert len(created_user["supplierIds"]) == 1

    operator = auth_app.test_client()
    _, operator_csrf = login_and_change_password(
        operator, "simple.operator", "Operator@123", "Operator@456"
    )
    visible_products = operator.get("/api/products")
    assert visible_products.status_code == 200
    # Product scoping removed: a warehouse user now sees every product.
    operator_product_ids = {item["id"] for item in visible_products.get_json()["data"]}
    admin_product_ids = {item["id"] for item in admin.get("/api/products").get_json()["data"]}
    assert operator_product_ids == admin_product_ids
    assert product["id"] in operator_product_ids
    assert operator.get("/api/dashboard").status_code == 403
    assert operator.post(
        f"/api/products/{product['id']}/code-sets",
        json={"prefix": "NOPE", "quantity": 1},
        headers={"X-CSRF-Token": operator_csrf},
    ).status_code == 403
    assert operator.put(
        "/api/records/1/status",
        json={"status": "PASSED"},
        headers={"X-CSRF-Token": operator_csrf},
    ).status_code == 403
    assert operator.put(
        "/api/records/status/bulk",
        json={"recordIds": [1], "status": "PASSED"},
        headers={"X-CSRF-Token": operator_csrf},
    ).status_code == 403
    assert operator.get(
        "/api/supplier-inventory-batches/1/movements"
    ).status_code == 403


def test_admin_can_search_security_audit_log_and_operator_cannot(auth_app):
    admin = auth_app.test_client()
    _, admin_csrf = login_and_change_password(
        admin, "admin", "Admin@12345", "Admin@98765"
    )
    created = admin.post(
        "/api/users",
        json={
            "username": "audit.operator",
            "displayName": "审计测试员",
            "password": "Operator@123",
            "role": "WAREHOUSE",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201, created.get_json()

    audit = admin.get("/api/audit-events?eventType=USER_CREATED&search=audit.operator")
    assert audit.status_code == 200, audit.get_json()
    data = audit.get_json()["data"]
    assert data["filteredTotal"] == 1
    assert data["total"] >= 3
    assert data["today"] >= 3
    assert data["operators"] >= 1
    assert data["items"][0]["eventType"] == "USER_CREATED"
    assert data["items"][0]["objectCode"] == "audit.operator"
    assert data["items"][0]["actorUsername"] == "admin"
    assert any(item["value"] == "USER_PASSWORD_CHANGED" for item in data["eventTypes"])

    operator = auth_app.test_client()
    login_and_change_password(
        operator, "audit.operator", "Operator@123", "Operator@456"
    )
    assert operator.get("/api/audit-events").status_code == 403


def test_authenticated_accounts_cannot_take_over_an_active_scan_station(auth_app):
    admin = auth_app.test_client()
    _, admin_csrf = login_and_change_password(
        admin, "admin", "Admin@12345", "Admin@98765"
    )
    family_response = admin.post(
        "/api/product-families",
        json={"productCode": "STATION_TEST", "name": "工位并发测试产品"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert family_response.status_code == 201, family_response.get_json()
    model_response = admin.post(
        "/api/product-models",
        json={
            "productFamilyId": family_response.get_json()["data"]["id"],
            "modelCode": "ST-01",
            "name": "ST-01 工位测试产品",
            "serialPrefix": "ST-01",
            "identitySource": "SCANNER",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert model_response.status_code == 201, model_response.get_json()
    station_model = model_response.get_json()["data"]
    for username, display_name in (
        ("operator.alpha", "装配员甲"),
        ("operator.beta", "装配员乙"),
    ):
        response = admin.post(
            "/api/users",
            json={
                "username": username,
                "displayName": display_name,
                "password": "Operator@123",
                "role": "WAREHOUSE",
                "productModelIds": [station_model["id"]],
                "supplierIds": [],
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 201, response.get_json()

    first = auth_app.test_client()
    _, first_csrf = login_and_change_password(
        first, "operator.alpha", "Operator@123", "Operator@456"
    )
    second = auth_app.test_client()
    _, second_csrf = login_and_change_password(
        second, "operator.beta", "Operator@123", "Operator@456"
    )

    models = first.get("/api/product-models").get_json()["data"]
    model = next(item for item in models if item["modelCode"] == "ST-01")
    machine_response = admin.post(
        "/api/machines",
        json={
            "sn": "ST-01-AUTH-STATION-00000001",
            "productModelId": model["id"],
            "productionDate": "20260717",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert machine_response.status_code == 201, machine_response.get_json()
    machine = machine_response.get_json()["data"]

    station_payload = {
        "stationId": "SHARED-STATION-01",
        "stationName": "共享工位 01",
        "code": machine["identificationCode"],
    }
    started = first.post(
        "/api/scan", json=station_payload, headers={"X-CSRF-Token": first_csrf}
    )
    assert started.status_code == 200, started.get_json()
    started_session = started.get_json()["data"]["session"]
    assert started_session["operatorName"] == "装配员甲"
    assert started_session["machine"]["productFamilyCode"] == "STATION_TEST"

    blocked_read = second.get("/api/scan/session?stationId=SHARED-STATION-01")
    assert blocked_read.status_code == 409
    assert "装配员甲" in blocked_read.get_json()["message"]
    blocked_write = second.post(
        "/api/scan", json=station_payload, headers={"X-CSRF-Token": second_csrf}
    )
    assert blocked_write.status_code == 409

    released = first.post(
        "/api/scan/reset",
        json={"stationId": "SHARED-STATION-01", "reason": "交接工位"},
        headers={"X-CSRF-Token": first_csrf},
    )
    assert released.status_code == 200, released.get_json()
    taken_over = second.post(
        "/api/scan", json=station_payload, headers={"X-CSRF-Token": second_csrf}
    )
    assert taken_over.status_code == 200, taken_over.get_json()
    assert taken_over.get_json()["data"]["session"]["operatorName"] == "装配员乙"


def test_production_mode_rejects_default_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("PTS_ENV", "production")
    monkeypatch.delenv("PTS_SECRET_KEY", raising=False)
    monkeypatch.delenv("PTS_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="PTS_SECRET_KEY"):
        create_app({"DATABASE": str(tmp_path / "unsafe.db"), "AUTH_DISABLED": False})

    monkeypatch.setenv(
        "PTS_SECRET_KEY", "replace-with-a-random-string-of-at-least-32-characters"
    )
    monkeypatch.setenv(
        "PTS_BOOTSTRAP_ADMIN_PASSWORD", "replace-with-a-strong-initial-password"
    )
    with pytest.raises(RuntimeError, match="PTS_SECRET_KEY"):
        create_app({"DATABASE": str(tmp_path / "placeholder.db"), "AUTH_DISABLED": False})

    safe = create_app(
        {
            "DATABASE": str(tmp_path / "safe.db"),
            "AUTH_DISABLED": False,
            "SECRET_KEY": "s" * 48,
            "BOOTSTRAP_ADMIN_PASSWORD": "ServerAdmin@98765",
        }
    )
    assert safe.test_client().get("/api/health").status_code == 200
