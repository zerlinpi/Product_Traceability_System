from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app import create_app
from batch_strategies import build_product_scenario
from capability_helpers import FakeLingxingService
from test_scan_gun_inbound import setup_production
from traceability.db import connect_database


def inbound_stock(client, production, quantity=6):
    response = client.post(
        "/api/scan-gun/inbound",
        json={"productionOrderId": production["id"], "quantity": quantity},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# Feature: batch-traceability, Property 74: 库存同步成功持久化并置为已推送
def test_inventory_sync_pushes_current_stock_and_persists_result(tmp_path):
    client, database_path, scenario, fake, _order, production = setup_production(tmp_path)
    inbound_stock(client, production, 9)
    # 库存同步 resolves the product's pending 收货单 (getOrderList, matched by SKU)
    # and then fast-receives it.
    fake.add_receipt("CR240729025", production["productModelCode"], 9)
    response = client.post("/api/inventory-sync")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["syncStatus"] == "PUSHED"
    assert data["syncedAt"] == "2026-07-22T10:30:00+08:00"
    assert data["itemCount"] == 1
    assert data["overall"]["syncStatus"] == "PUSHED"
    assert data["overall"]["total"] == 1
    assert data["overall"]["pushed"] == 1
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["productModelId"] == scenario["product"]["id"]
    assert item["sku"] == production["productModelCode"]
    assert item["quantity"] == 9
    assert item["syncStatus"] == "PUSHED"
    assert item["syncedQuantity"] == 9
    assert item["lingxingId"] == "CR240729025"
    assert item["syncedAt"] == "2026-07-22T10:30:00+08:00"
    # getOrderList first, then fastReceive with the resolved 收货单号 + 子项id.
    assert [op for op, _ in fake.calls] == ["receipt_list", "inventory_sync"]
    fast_receive_payload = fake.calls[1][1]
    assert fast_receive_payload["order_sn"] == "CR240729025"
    assert fast_receive_payload["item_list"] == [
        {"id": 1, "product_good_num": 9, "product_bad_num": 0}
    ]
    database = connect_database(database_path)
    try:
        settings = dict(
            database.execute(
                "SELECT setting_key, setting_value FROM app_settings WHERE setting_key LIKE 'inventory_sync.%'"
            ).fetchall()
        )
        assert settings["inventory_sync.status"] == "PUSHED"
        assert settings["inventory_sync.in_progress"] == "0"
        assert database.execute("SELECT on_hand FROM product_stock").fetchone()[0] == 9
        assert database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='INVENTORY_SYNC_PUSHED'"
        ).fetchone()[0] == 1
    finally:
        database.close()

# Feature: batch-traceability, Property 75: 库存同步凭据缺失降级、失败保库存与进行中守卫
def test_inventory_sync_missing_credentials_has_zero_calls_and_no_local_change(tmp_path):
    client, database_path, _scenario, fake, _order, production = setup_production(tmp_path)
    fake.configured = False
    inbound_stock(client, production, 4)
    database = connect_database(database_path)
    try:
        before_stock = [tuple(row) for row in database.execute("SELECT * FROM product_stock")]
        before_settings = [tuple(row) for row in database.execute("SELECT * FROM app_settings")]
    finally:
        database.close()
    response = client.post("/api/inventory-sync")
    assert response.status_code == 503
    assert "领星凭据未配置" in response.get_json()["message"]
    assert fake.calls == []
    database = connect_database(database_path)
    try:
        assert [tuple(row) for row in database.execute("SELECT * FROM product_stock")] == before_stock
        assert [tuple(row) for row in database.execute("SELECT * FROM app_settings")] == before_settings
    finally:
        database.close()


def test_inventory_sync_failure_and_in_progress_guard_preserve_stock(tmp_path):
    client, database_path, _scenario, fake, _order, production = setup_production(tmp_path)
    inbound_stock(client, production, 11)
    # A 收货单 exists, but the fast-receive call fails.
    fake.add_receipt("CR-FAIL", production["productModelCode"], 11)
    fake.failures.add("inventory_sync")
    failed = client.post("/api/inventory-sync")
    assert failed.status_code == 502
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT on_hand FROM product_stock").fetchone()[0] == 11
        status = database.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key='inventory_sync.status'"
        ).fetchone()[0]
        assert status == "FAILED"
        database.execute(
            """
            INSERT INTO app_settings(setting_key, setting_value, is_secret, updated_at)
            VALUES ('inventory_sync.in_progress', '1', 0, 'now')
            ON CONFLICT(setting_key) DO UPDATE SET setting_value='1'
            """
        )
    finally:
        database.close()

    calls_before = len(fake.calls)
    guarded = client.post("/api/inventory-sync")
    assert guarded.status_code == 409
    assert "正在进行中" in guarded.get_json()["message"]
    assert len(fake.calls) == calls_before
    database = connect_database(database_path)
    try:
        assert database.execute("SELECT on_hand FROM product_stock").fetchone()[0] == 11
        assert database.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key='inventory_sync.in_progress'"
        ).fetchone()[0] == "1"
    finally:
        database.close()


class BlockingInventoryService(FakeLingxingService):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def push(self, operation: str, payload: dict) -> dict:
        # Block on the first external call so a concurrent sync races the guard,
        # then return the normal envelopes for the getOrderList / fastReceive chain.
        self.started.set()
        assert self.release.wait(10), "测试未释放库存同步阻塞调用"
        return super().push(operation, payload)


def test_concurrent_inventory_sync_makes_at_most_one_external_call(tmp_path):
    database_path = tmp_path / "inventory-concurrent.db"
    service = BlockingInventoryService()
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "LINGXING_SERVICE_FACTORY": lambda _database: service,
        }
    )
    setup_client = app.test_client()
    scenario = build_product_scenario(setup_client, "STOCK-CONCURRENT", [1], 100)
    order_response = setup_client.post(
        "/api/purchase-orders",
        json={
            "supplierId": scenario["suppliers"][0]["id"],
            "partTypeId": scenario["parts"][0]["id"],
            "quantity": 4,
        },
    )
    production = setup_client.post(
        "/api/production-orders",
        json={"purchaseOrderId": order_response.get_json()["data"]["id"]},
    ).get_json()["data"]
    inbound_stock(setup_client, production, 4)
    service.add_receipt("CR-CONC", production["productModelCode"], 4)

    def first_sync():
        return app.test_client().post("/api/inventory-sync")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_sync)
        assert service.started.wait(10)
        second = app.test_client().post("/api/inventory-sync")
        assert second.status_code == 409
        assert "正在进行中" in second.get_json()["message"]
        service.release.set()
        first = first_future.result(timeout=10)
    assert first.status_code == 200, first.get_json()
    # Only the first sync reached the external API (getOrderList + fastReceive);
    # the concurrent one was rejected by the in-progress guard before any call.
    assert [op for op, _ in service.calls] == ["receipt_list", "inventory_sync"]


# ---------------------------------------------------------------------------
# Per-product sync board + selected-subset sync (operations inventory sync shows
# each product's sync status and supports batch / select sync).
# ---------------------------------------------------------------------------

from test_inbound_receipts import create_order  # noqa: E402
from test_production_orders import create_production  # noqa: E402
from test_purchase_orders import setup_case  # noqa: E402


def _stocked_product(client, suffix, quantity):
    """Create a product + purchase/production order and receive stock for it."""
    scenario = build_product_scenario(client, suffix, [1], 100)
    order = create_order(client, scenario, quantity)
    production = create_production(client, order)
    inbound_stock(client, production, quantity)
    return scenario["product"], production


def test_inventory_sync_get_reports_per_product_status(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 6)
    production = create_production(client, order)
    inbound_stock(client, production, 6)

    board = client.get("/api/inventory-sync")
    assert board.status_code == 200, board.get_json()
    data = board.get_json()["data"]
    assert data["overall"]["total"] == 1
    assert data["overall"]["pending"] == 1
    assert data["overall"]["pushed"] == 0
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["productModelId"] == scenario["product"]["id"]
    assert item["quantity"] == 6
    assert item["syncStatus"] == "PENDING"
    assert item["syncedQuantity"] == 0
    assert item["lingxingId"] is None

    # After a full sync the same board reports the product as PUSHED.
    _fake.add_receipt("CR-6", scenario["product"]["productCode"], 6)
    assert client.post("/api/inventory-sync").status_code == 200
    after = client.get("/api/inventory-sync").get_json()["data"]
    assert after["overall"]["pushed"] == 1
    assert after["items"][0]["syncStatus"] == "PUSHED"
    assert after["items"][0]["syncedQuantity"] == 6
    assert after["items"][0]["lingxingId"] == "CR-6"


def test_inventory_sync_selected_subset_only_pushes_chosen_products(tmp_path):
    client, database_path, scenario_a, fake = setup_case(tmp_path)
    # Product A comes from the shared scenario; add a second stocked product B.
    order_a = create_order(client, scenario_a, 5)
    production_a = create_production(client, order_a)
    inbound_stock(client, production_a, 5)
    product_a = scenario_a["product"]
    product_b, _production_b = _stocked_product(client, "STOCK-B", 8)
    fake.add_receipt("CR-A", product_a["productCode"], 5, item_id=1)
    fake.add_receipt("CR-B", product_b["productCode"], 8, item_id=2)

    # Sync only product B.
    response = client.post(
        "/api/inventory-sync", json={"productModelIds": [product_b["id"]]}
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["itemCount"] == 1

    # getOrderList once, then a single fastReceive carrying only product B's line.
    assert [op for op, _ in fake.calls] == ["receipt_list", "inventory_sync"]
    fast_receive_payload = fake.calls[1][1]
    assert fast_receive_payload["order_sn"] == "CR-B"
    assert fast_receive_payload["item_list"] == [
        {"id": 2, "product_good_num": 8, "product_bad_num": 0}
    ]

    board = client.get("/api/inventory-sync").get_json()["data"]
    by_id = {item["productModelId"]: item for item in board["items"]}
    assert by_id[product_b["id"]]["syncStatus"] == "PUSHED"
    assert by_id[product_a["id"]]["syncStatus"] == "PENDING"
    assert board["overall"]["pushed"] == 1
    assert board["overall"]["pending"] == 1

    database = connect_database(database_path)
    try:
        rows = dict(
            database.execute(
                "SELECT product_model_id, sync_status FROM product_stock_sync"
            ).fetchall()
        )
        assert rows == {product_b["id"]: "PUSHED"}
    finally:
        database.close()


def test_inventory_sync_selected_unknown_product_is_rejected(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 4)
    production = create_production(client, order)
    inbound_stock(client, production, 4)
    response = client.post(
        "/api/inventory-sync", json={"productModelIds": [999999]}
    )
    assert response.status_code == 404
    assert "没有库存记录" in response.get_json()["message"]


def test_inventory_sync_partial_when_some_products_lack_receipts(tmp_path):
    client, database_path, scenario_a, fake = setup_case(tmp_path)
    order_a = create_order(client, scenario_a, 5)
    production_a = create_production(client, order_a)
    inbound_stock(client, production_a, 5)
    product_a = scenario_a["product"]
    product_b, _production_b = _stocked_product(client, "STOCK-PARTIAL", 8)
    # Only product A has a pending 收货单; B cannot be fast-received.
    fake.add_receipt("CR-A", product_a["productCode"], 5)

    response = client.post("/api/inventory-sync")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["syncStatus"] == "PARTIAL"
    assert data["itemCount"] == 1  # only A received

    board = client.get("/api/inventory-sync").get_json()["data"]
    by_id = {item["productModelId"]: item for item in board["items"]}
    assert by_id[product_a["id"]]["syncStatus"] == "PUSHED"
    assert by_id[product_b["id"]]["syncStatus"] == "FAILED"
    assert by_id[product_b["id"]]["pushError"]

    database = connect_database(database_path)
    try:
        rows = dict(
            database.execute(
                "SELECT product_model_id, sync_status FROM product_stock_sync"
            ).fetchall()
        )
        assert rows[product_a["id"]] == "PUSHED"
        assert rows[product_b["id"]] == "FAILED"
    finally:
        database.close()
