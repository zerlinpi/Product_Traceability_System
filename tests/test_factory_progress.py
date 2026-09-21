from __future__ import annotations

from test_inbound_receipts import create_order
from test_production_orders import create_production
from test_purchase_orders import setup_case


# Feature: batch-traceability, Property 76: 工厂进度映射
def test_factory_progress_maps_order_generation_and_accumulated_inbound(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 20)
    initial = client.get(f"/api/purchase-orders/{order['id']}/factory-progress")
    assert initial.status_code == 200
    assert initial.get_json()["data"]["productionOrderGenerated"] is False
    assert initial.get_json()["data"]["latestQuantity"] == 0

    production = create_production(client, order)
    generated = client.get(f"/api/purchase-orders/{order['id']}/factory-progress")
    assert generated.get_json()["data"]["productionOrderGenerated"] is True
    assert generated.get_json()["data"]["latestQuantity"] == 0

    for quantity in (3, 5, 7):
        response = client.post(
            "/api/scan-gun/inbound",
            json={"productionOrderId": production["id"], "quantity": quantity},
        )
        assert response.status_code == 201, response.get_json()
    progress = client.get(f"/api/purchase-orders/{order['id']}/factory-progress")
    assert progress.get_json()["data"]["productionOrderId"] == production["id"]
    assert progress.get_json()["data"]["latestQuantity"] == 15

    missing = client.get("/api/purchase-orders/999999/factory-progress")
    assert missing.status_code == 404
    assert "采购订单不存在" in missing.get_json()["message"]

