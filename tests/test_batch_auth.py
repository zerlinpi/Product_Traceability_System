from capability_helpers import (
    bootstrap_admin,
    insert_user,
    login,
    make_auth_app,
    post,
    seed_scenario,
)
from traceability.db import connect_database


def table_counts(database_path):
    database = connect_database(database_path)
    try:
        return {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "production_batches",
                "batch_trace_records",
                "supplier_inventory_movements",
            )
        }
    finally:
        database.close()


# Product scoping removed: a warehouse user operates every product, so any
# warehouse account can register and query any batch without a per-product grant.
def test_warehouse_operates_any_product_without_per_product_scope(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    scenario = seed_scenario(admin, admin_csrf, "BATCH-AUTH")
    generated = post(
        admin,
        admin_csrf,
        "/api/production-batches",
        {"productModelId": scenario["product"]["id"], "quantity": 4, "prefix": "BAUTH"},
    )
    # A warehouse user with no per-product grant at all.
    insert_user(database_path, "warehouse.any", "WAREHOUSE")
    warehouse, warehouse_csrf = login(app, "warehouse.any")

    # Warehouse can register the batch even without any per-product grant.
    accepted = warehouse.post(
        "/api/batch-entry/scan",
        json={"code": generated["batchCode"]},
        headers={"X-CSRF-Token": warehouse_csrf},
    )
    assert accepted.status_code == 201, accepted.get_json()
    assert accepted.get_json()["data"]["qualityStatus"] == "ASSEMBLED"
    assert table_counts(database_path)["batch_trace_records"] == 1

    # And it can query any batch.
    assert warehouse.post(
        "/api/batch-trace/query",
        json={"code": generated["batchCode"]},
        headers={"X-CSRF-Token": warehouse_csrf},
    ).status_code == 200


# Feature: batch-traceability, Property 28: 未认证拒绝且无副作用
def test_anonymous_batch_operations_are_rejected_without_side_effects(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    scenario = seed_scenario(admin, admin_csrf, "BATCH-ANON")
    generated = post(
        admin,
        admin_csrf,
        "/api/production-batches",
        {"productModelId": scenario["product"]["id"], "quantity": 3, "prefix": "BANON"},
    )
    anonymous = app.test_client()
    before = table_counts(database_path)
    assert anonymous.post("/api/production-batches", json={}).status_code == 401
    assert anonymous.post(
        "/api/batch-entry/scan", json={"code": generated["batchCode"]}
    ).status_code == 401
    assert anonymous.post(
        "/api/batch-trace/query", json={"code": generated["batchCode"]}
    ).status_code == 401
    assert table_counts(database_path) == before

