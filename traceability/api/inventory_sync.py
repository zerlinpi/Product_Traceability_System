"""Inventory sync (库存同步) — HTTP layer.

``GET /api/inventory-sync`` is the operations board: current stock per product
with its last Lingxing sync status. ``POST /api/inventory-sync`` runs the sync
for every product or a selected subset: it resolves each product's pending
收货单, fast-receives them, records PUSHED / FAILED per product, and is guarded
against concurrent runs (a guard older than ``INVENTORY_SYNC_GUARD_TIMEOUT`` is
taken over, and the takeover is audited).

The Lingxing calls and the bookkeeping helpers live in
``traceability/inventory_sync.py``; the transactions and audit events stay in
the route.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import require_operations
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.inventory_sync import (
    INVENTORY_SYNC_GUARD_TIMEOUT,
    fast_receive_product,
    fetch_pending_receipts_by_sku,
    inventory_sync_items,
    inventory_sync_overall,
    mark_product_stock_sync_failed,
    mark_product_stock_synced,
    upsert_app_setting,
)
from traceability.lingxing import LingxingError
from traceability.lingxing_writes import (
    ensure_lingxing_operation_ready,
    guard_is_stale,
    lingxing_service,
)
from traceability.responses import success
from traceability.validators import business_id

inventory_sync_bp = Blueprint("inventory_sync", __name__)


@inventory_sync_bp.get("/api/inventory-sync")
def get_inventory_sync():
    # Per-product sync board: current stock plus each product's last Lingxing
    # sync status/time, and an overall summary. Drives the operations
    # inventory-sync page (全部同步 / 同步所选).
    require_operations()
    database = get_db()
    items = inventory_sync_items(database)
    return success(
        {"overall": inventory_sync_overall(database, items), "items": items}
    )


@inventory_sync_bp.post("/api/inventory-sync")
def sync_inventory_to_lingxing():
    # 库存同步 receives each product's goods into Lingxing through 快捷入库
    # (fastReceive): it resolves the product's pending 收货单 via 查询收货单列表
    # (getOrderList, matched by SKU) and then fast-receives every outstanding
    # line. Per-product PUSHED/FAILED status is recorded so operations sees
    # the corresponding order sync; a subset can be synced via
    # ``productModelIds`` and the whole run is guarded against concurrency.
    require_operations()
    database = get_db()
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("productModelIds")
    selected_ids: list[int] | None = None
    if raw_ids not in (None, ""):
        if not isinstance(raw_ids, list):
            raise ApiError("productModelIds 必须是数组")
        parsed = [business_id(raw, "产品") for raw in raw_ids]
        selected_ids = list(dict.fromkeys(parsed))
        if not selected_ids:
            raise ApiError("请选择至少一个产品")
    service = lingxing_service(database)
    # Both the 收货单 lookup and the fast-receive must be reachable.
    ensure_lingxing_operation_ready(service, "receipt_list")
    ensure_lingxing_operation_ready(service, "inventory_sync")

    # Resolve the stock rows in scope (a selected subset or every product)
    # before acquiring the in-progress guard so a bad selection fails fast.
    base_query = (
        "SELECT ps.product_model_id, ps.on_hand, pm.model_code, pm.name "
        "FROM product_stock ps JOIN product_models pm ON pm.id = ps.product_model_id"
    )
    if selected_ids is None:
        stock_rows = database.execute(base_query + " ORDER BY pm.model_code").fetchall()
    else:
        placeholders = ",".join("?" for _ in selected_ids)
        stock_rows = database.execute(
            base_query
            + f" WHERE ps.product_model_id IN ({placeholders}) ORDER BY pm.model_code",
            selected_ids,
        ).fetchall()
        found = {row["product_model_id"] for row in stock_rows}
        missing = [pid for pid in selected_ids if pid not in found]
        if missing:
            raise ApiError("所选产品没有库存记录，无法同步", 404)

    scope_ids = [row["product_model_id"] for row in stock_rows]
    timestamp = current_app.config["NOW_PROVIDER"]()
    database.execute("BEGIN IMMEDIATE")
    try:
        guard = database.execute(
            "SELECT setting_value, updated_at FROM app_settings "
            "WHERE setting_key = 'inventory_sync.in_progress'"
        ).fetchone()
        recovered_stale_guard = False
        if guard and guard["setting_value"] == "1":
            if not guard_is_stale(
                guard["updated_at"], timestamp, INVENTORY_SYNC_GUARD_TIMEOUT
            ):
                raise ApiError("库存同步正在进行中", 409)
            # The previous run never finished; take the guard over and record it.
            recovered_stale_guard = True
        upsert_app_setting(database, "inventory_sync.in_progress", "1", timestamp)
        if recovered_stale_guard:
            record_audit_event(
                database,
                "INVENTORY_SYNC_GUARD_RECOVERED",
                "INVENTORY_SYNC",
                "PRODUCT_STOCK",
                reason="上次库存同步未正常结束，已自动接管",
                payload={"staleSince": guard["updated_at"]},
                occurred_at=timestamp,
            )
        database.commit()
    except Exception:
        database.rollback()
        raise

    def release_guard(status: str, error_message: str, results: list[dict[str, Any]]) -> None:
        finished_at = current_app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        for key, value in {
            "inventory_sync.in_progress": "0",
            "inventory_sync.status": status,
            "inventory_sync.synced_at": finished_at,
            "inventory_sync.error": error_message,
        }.items():
            upsert_app_setting(database, key, value, finished_at)
        event_type = "INVENTORY_SYNC_PUSHED" if status == "PUSHED" else "INVENTORY_SYNC_FAILED"
        record_audit_event(
            database,
            event_type,
            "INVENTORY_SYNC",
            "PRODUCT_STOCK",
            reason=error_message,
            payload={
                "result": status,
                "scope": "SELECTED" if selected_ids is not None else "ALL",
                "productModelIds": scope_ids,
                "results": results,
            },
            occurred_at=finished_at,
        )
        database.commit()

    # Resolve the pending 收货单 once (scoped to the products being synced so a
    # subset sync doesn't scan every pending 收货单), then fast-receive each line.
    scope_skus = {
        str(row["model_code"]).upper() for row in stock_rows if row["model_code"]
    }
    try:
        by_sku = fetch_pending_receipts_by_sku(service, scope_skus)
    except Exception as error:
        message = error.message if isinstance(error, (ApiError, LingxingError)) else str(error)
        finished_at = current_app.config["NOW_PROVIDER"]()
        database.execute("BEGIN IMMEDIATE")
        for product_model_id in scope_ids:
            mark_product_stock_sync_failed(database, product_model_id, message, finished_at)
        database.commit()
        release_guard("FAILED", message, [])
        if isinstance(error, (ApiError, LingxingError)):
            raise
        raise LingxingError(f"领星收货单查询失败：{message}") from error

    results: list[dict[str, Any]] = []
    succeeded = 0
    finished_at = current_app.config["NOW_PROVIDER"]()
    database.execute("BEGIN IMMEDIATE")
    try:
        for row in stock_rows:
            try:
                order_sns, good_total = fast_receive_product(
                    service, row["model_code"], by_sku
                )
                mark_product_stock_synced(
                    database,
                    row["product_model_id"],
                    good_total,
                    ",".join(order_sns),
                    finished_at,
                )
                succeeded += 1
                results.append(
                    {
                        "productModelId": row["product_model_id"],
                        "status": "PUSHED",
                        "orderSns": order_sns,
                        "goodNum": good_total,
                    }
                )
            except (ApiError, LingxingError) as error:
                mark_product_stock_sync_failed(
                    database, row["product_model_id"], error.message, finished_at
                )
                results.append(
                    {
                        "productModelId": row["product_model_id"],
                        "status": "FAILED",
                        "message": error.message,
                    }
                )
        database.commit()
    except Exception:
        database.rollback()
        release_guard("FAILED", "库存同步异常", results)
        raise

    failed = [item for item in results if item["status"] == "FAILED"]
    if succeeded == 0:
        # Nothing was received (every scoped product failed) — surface the
        # first reason and leave local stock untouched.
        message = failed[0]["message"] if failed else "没有可同步的收货单"
        release_guard("FAILED", message, results)
        raise LingxingError(f"领星快捷入库失败：{message}")
    overall_status = "PUSHED" if not failed else "PARTIAL"
    release_guard(
        overall_status,
        failed[0]["message"] if failed else "",
        results,
    )
    items = inventory_sync_items(database)
    return success(
        {
            "syncStatus": overall_status,
            "syncedAt": finished_at,
            "itemCount": succeeded,
            "results": results,
            "overall": inventory_sync_overall(database, items),
            "items": items,
        }
    )
