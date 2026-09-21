from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app import create_app
from batch_strategies import build_product_scenario
from capability_helpers import FakeLingxingService
from test_inbound_receipts import create_order
from test_purchase_orders import setup_case
from traceability.db import connect_database


def create_receipt(client, order, quantity=5):
    response = client.post(
        "/api/inbound-receipts",
        json={"purchaseOrderId": order["id"], "quantity": quantity},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


# Feature: batch-traceability, Property 46: 推送成功持久化并置为已推送
# Feature: batch-traceability, Property 47: 推送幂等（已推送不重复创建）
def test_purchase_and_receipt_push_persist_ids_and_are_idempotent(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    receipt = create_receipt(client, order)

    pushed_order = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert pushed_order.status_code == 200, pushed_order.get_json()
    assert pushed_order.get_json()["data"]["syncStatus"] == "PUSHED"
    # 采购单下单 returns no object id, so the 采购单号 we submitted is recorded.
    assert pushed_order.get_json()["data"]["lingxingPoId"] == order["poNo"]

    pushed_receipt = client.post(f"/api/inbound-receipts/{receipt['id']}/push")
    assert pushed_receipt.status_code == 200, pushed_receipt.get_json()
    assert pushed_receipt.get_json()["data"]["syncStatus"] == "PUSHED"
    assert pushed_receipt.get_json()["data"]["lingxingInboundId"] == "LX-IN-1"

    assert client.post(f"/api/purchase-orders/{order['id']}/push").status_code == 200
    assert client.post(f"/api/inbound-receipts/{receipt['id']}/push").status_code == 200
    assert [name for name, _payload in fake.calls] == ["purchase_order", "inbound_receipt"]

    database = connect_database(database_path)
    try:
        stored_order = database.execute(
            "SELECT sync_status, lingxing_po_id, pushed_at FROM purchase_orders WHERE id=?",
            (order["id"],),
        ).fetchone()
        stored_receipt = database.execute(
            "SELECT sync_status, lingxing_inbound_id, pushed_at FROM inbound_receipts WHERE id=?",
            (receipt["id"],),
        ).fetchone()
        assert tuple(stored_order) == ("PUSHED", order["poNo"], "2026-07-22T10:30:00+08:00")
        assert tuple(stored_receipt) == ("PUSHED", "LX-IN-1", "2026-07-22T10:30:00+08:00")
    finally:
        database.close()


def test_missing_business_endpoint_keeps_purchase_order_pending(tmp_path):
    database_path = tmp_path / "missing-endpoint.db"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "LINGXING_CREDENTIALS": {
                "appId": "1234567890abcdef",
                "appSecret": "test-secret",
            },
            "LINGXING_ENDPOINTS": {
                "token": "mock://token",
                "refresh_token": "mock://refresh",
                "purchase_order": "",
                "inbound_receipt": "",
                "inventory_sync": "",
            },
        }
    )
    client = app.test_client()
    scenario = build_product_scenario(client, "NO-ENDPOINT", [1], 100)
    order = create_order(client, scenario)

    response = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert response.status_code == 503
    assert "领星接口地址未配置" in response.get_json()["message"]

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT sync_status, push_in_progress, push_error FROM purchase_orders WHERE id=?",
            (order["id"],),
        ).fetchone()
        assert tuple(row) == ("PENDING", 0, "")
        assert database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='PO_PUSH_FAILED'"
        ).fetchone()[0] == 0
    finally:
        database.close()


# Feature: batch-traceability, Property 48: 推送失败标记为 FAILED 且无部分修改
def test_failed_push_keeps_record_and_marks_failed(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    fake.failures.add("purchase_order")
    order = create_order(client, scenario)
    response = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert response.status_code == 502
    assert "mock purchase_order failed" in response.get_json()["message"]

    database = connect_database(database_path)
    try:
        row = database.execute(
            "SELECT sync_status, lingxing_po_id, push_in_progress, push_error FROM purchase_orders WHERE id=?",
            (order["id"],),
        ).fetchone()
        assert tuple(row) == ("FAILED", "", 0, "mock purchase_order failed")
        assert database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='PO_PUSH_FAILED'"
        ).fetchone()[0] == 1
    finally:
        database.close()


# Feature: batch-traceability, Property 49: 领星入库前置条件——采购订单须已推送
# Feature: batch-traceability, Property 50: 领星入库引用不存在即拒绝
def test_receipt_push_requires_pushed_order_and_existing_receipt(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    receipt = create_receipt(client, order)
    before = client.get(f"/api/inbound-receipts/{receipt['id']}").get_json()["data"]

    response = client.post(f"/api/inbound-receipts/{receipt['id']}/push")
    assert response.status_code == 409
    assert "先成功推送采购订单" in response.get_json()["message"]
    assert client.post("/api/inbound-receipts/999999/push").status_code == 404
    assert fake.calls == []
    after = client.get(f"/api/inbound-receipts/{receipt['id']}").get_json()["data"]
    assert after == before

    database = connect_database(database_path)
    try:
        assert database.execute(
            "SELECT sync_status FROM inbound_receipts WHERE id=?", (receipt["id"],)
        ).fetchone()[0] == "PENDING"
    finally:
        database.close()


# Feature: batch-traceability, Property 51: 进行中守卫防止并发重复推送
def test_in_progress_guard_prevents_another_external_call(tmp_path):
    client, database_path, scenario, fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    database = connect_database(database_path)
    try:
        database.execute(
            "UPDATE purchase_orders SET push_in_progress=1 WHERE id=?", (order["id"],)
        )
    finally:
        database.close()
    response = client.post(f"/api/purchase-orders/{order['id']}/push")
    assert response.status_code == 409
    assert "正在进行中" in response.get_json()["message"]
    assert fake.calls == []


class BlockingPurchaseService(FakeLingxingService):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def push(self, operation: str, payload: dict) -> dict:
        self.calls.append((operation, payload))
        self.started.set()
        assert self.release.wait(10), "测试未释放领星阻塞调用"
        return {"ok": True, "data": {"poId": "LX-PO-CONCURRENT"}}


def test_concurrent_purchase_push_makes_at_most_one_external_call(tmp_path):
    database_path = tmp_path / "po-concurrent.db"
    service = BlockingPurchaseService()
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "LINGXING_SERVICE_FACTORY": lambda _database: service,
        }
    )
    setup_client = app.test_client()
    scenario = build_product_scenario(setup_client, "PO-CONCURRENT", [1], 100)
    order = create_order(setup_client, scenario)

    def first_push():
        return app.test_client().post(f"/api/purchase-orders/{order['id']}/push")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_push)
        assert service.started.wait(10)
        second = app.test_client().post(f"/api/purchase-orders/{order['id']}/push")
        assert second.status_code == 409
        assert "正在进行中" in second.get_json()["message"]
        service.release.set()
        first = first_future.result(timeout=10)
    assert first.status_code == 200, first.get_json()
    assert len(service.calls) == 1
