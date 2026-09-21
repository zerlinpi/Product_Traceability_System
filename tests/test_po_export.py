from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from app import PURCHASE_ORDER_EXPORT_COLUMNS
from test_inbound_receipts import create_order
from test_purchase_orders import setup_case
from traceability.db import connect_database


def read_export(response):
    workbook = load_workbook(BytesIO(response.data), data_only=True)
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    values = [cell.value for cell in sheet[2]]
    formats = [cell.number_format for cell in sheet[2]]
    return headers, values, formats


# Feature: batch-traceability, Property 71: 采购单导出列精确性
def test_purchase_order_export_has_exact_columns_in_exact_order(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 27)
    response = client.get(f"/api/purchase-orders/{order['id']}/export")
    assert response.status_code == 200, response.get_json()
    assert response.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    headers, _values, _formats = read_export(response)
    assert headers == PURCHASE_ORDER_EXPORT_COLUMNS
    assert len(headers) == 48


# Feature: batch-traceability, Property 72: 采购单导出必填列填充与类型
def test_purchase_order_export_required_values_and_numeric_types(tmp_path):
    client, _database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario, 37)
    response = client.get(f"/api/purchase-orders/{order['id']}/export")
    headers, values, formats = read_export(response)
    row = dict(zip(headers, values))
    required = [
        "标识号",
        "供应商",
        "含税",
        "费用分配方式",
        "采购币种",
        "采购仓库",
        "SKU",
        "实际采购量",
        "含税单价",
    ]
    assert all(row[name] not in {None, ""} for name in required)
    assert isinstance(row["实际采购量"], int) and row["实际采购量"] == 37
    assert isinstance(row["含税单价"], (int, float)) and row["含税单价"] > 0
    assert formats[headers.index("含税单价")] == "0.00"


# Feature: batch-traceability, Property 73: 采购单导出校验拒绝且无副作用
def test_export_rejects_missing_order_or_required_value_without_mutation(tmp_path):
    client, database_path, scenario, _fake = setup_case(tmp_path)
    order = create_order(client, scenario)
    assert client.get("/api/purchase-orders/999999/export").status_code == 404

    database = connect_database(database_path)
    try:
        database.execute(
            "UPDATE part_types SET part_code='' WHERE id=?", (scenario["parts"][0]["id"],)
        )
        before = [tuple(row) for row in database.execute("SELECT * FROM purchase_orders")]
    finally:
        database.close()
    response = client.get(f"/api/purchase-orders/{order['id']}/export")
    assert response.status_code == 400
    assert "SKU" in response.get_json()["message"]
    database = connect_database(database_path)
    try:
        assert [tuple(row) for row in database.execute("SELECT * FROM purchase_orders")] == before
    finally:
        database.close()

