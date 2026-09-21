"""The admin board must reflect the current batch flow, not the retired per-unit one.

走步机 moved to batch traceability, so ``trace_records``/``machines`` stop growing
and the old board showed zeros while real work piled up. These tests pin the batch
metrics and the flow backlog the board reports:

采购单 → 生产订单(溯源码) → 批次登记 → 质量放行 → 成品入库 → 库存同步
"""

from __future__ import annotations

from test_inbound_receipts import create_order
from test_production_orders import create_production
from test_purchase_orders import setup_case


def counts(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["counts"]


def test_board_tracks_each_step_of_the_batch_flow(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)

    # A purchase order with no production order yet is 第 1 步 backlog.
    order = create_order(client, scenario, 10)
    initial = counts(client)
    assert initial["pendingProductionOrders"] == 1
    assert initial["productionBatches"] == 0
    assert initial["productionOrders"] == 0
    assert initial["finishedGoodsOnHand"] == 0

    # Generating the production order mints a batch; it is now awaiting 登记.
    production = create_production(client, order)
    after_order = counts(client)
    assert after_order["pendingProductionOrders"] == 0
    assert after_order["productionOrders"] == 1
    assert after_order["productionBatches"] == 1
    assert after_order["todayBatches"] == 1
    assert after_order["unregisteredBatches"] == 1
    assert after_order["registeredBatches"] == 0
    assert after_order["pendingStockIn"] == 1

    # 批次登记 moves it into the 待检 queue.
    registered = client.post(
        "/api/batch-entry/scan", json={"code": production["identificationCode"]}
    )
    assert registered.status_code == 201, registered.get_json()
    record = registered.get_json()["data"]
    after_registration = counts(client)
    assert after_registration["unregisteredBatches"] == 0
    assert after_registration["registeredBatches"] == 1
    assert after_registration["batchAssembled"] == 1
    assert after_registration["batchPassed"] == 0
    assert after_registration["batchHold"] == 0

    # The board surfaces that queue so quality can be handled from the dashboard.
    queue = client.get("/api/dashboard").get_json()["data"]["batchQualityQueue"]
    assert [item["id"] for item in queue] == [record["id"]]
    assert queue[0]["batchCode"] == production["productionQrCode"]
    assert queue[0]["productName"] == scenario["product"]["name"]

    # 质量放行 then 成品入库 clears the remaining steps.
    assert client.post(f"/api/batch-trace-records/{record['id']}/pass").status_code == 200
    after_release = counts(client)
    assert after_release["batchPassed"] == 1
    assert after_release["batchAssembled"] == 0

    inbound = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": 10},
    )
    assert inbound.status_code == 201, inbound.get_json()
    after_stock_in = counts(client)
    assert after_stock_in["pendingStockIn"] == 0
    assert after_stock_in["finishedGoodsOnHand"] == 10
    # Stock exists but was never pushed to Lingxing yet.
    assert after_stock_in["inventorySyncPending"] == 1


def test_board_counts_held_batches(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 5)
    production = create_production(client, order)
    record = client.post(
        "/api/batch-entry/scan", json={"code": production["identificationCode"]}
    ).get_json()["data"]
    held = client.post(
        f"/api/batch-trace-records/{record['id']}/hold", json={"reason": "外观待复核"}
    )
    assert held.status_code == 200, held.get_json()

    data = client.get("/api/dashboard").get_json()["data"]
    assert data["counts"]["batchHold"] == 1
    assert data["counts"]["batchAssembled"] == 0
    # A held batch stays on the board so it gets closed out.
    assert [item["qualityStatus"] for item in data["batchQualityQueue"]] == ["HOLD"]
