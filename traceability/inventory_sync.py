"""Inventory sync (库存同步): the Lingxing receive run and its bookkeeping.

Operations push finished-goods stock to Lingxing by receiving each product's
pending 收货单 through 快捷入库 (fastReceive); the pending 收货单 are resolved
with 查询收货单列表 (getOrderList) and matched to products by SKU. This module
holds the parts of that run that are not HTTP:

* the in-progress guard and the last-run summary, kept as ``inventory_sync.*``
  rows in ``app_settings`` (``upsert_app_setting``, ``inventory_sync_settings``);
* the per-product board: current stock joined to each product's last sync
  (``inventory_sync_items``, ``inventory_sync_overall``) and the PUSHED / FAILED
  bookkeeping in ``product_stock_sync``;
* the two Lingxing calls (``fetch_pending_receipts_by_sku``,
  ``fast_receive_product``).

The run itself (scoping, taking the guard, the transactions and the audit
events) stays in ``traceability/api/inventory_sync.py``. The push plumbing
shared with purchase orders and inbound receipts is ``lingxing_writes.py``.

Extracted from ``app.py`` when the inventory-sync routes moved to a blueprint.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import timedelta
from typing import Any

from traceability.auth import current_actor_id, current_actor_name
from traceability.errors import ApiError

__all__ = [
    "INVENTORY_SYNC_GUARD_TIMEOUT",
    "fast_receive_product",
    "fetch_pending_receipts_by_sku",
    "inventory_sync_items",
    "inventory_sync_overall",
    "inventory_sync_settings",
    "mark_product_stock_sync_failed",
    "mark_product_stock_synced",
    "upsert_app_setting",
]


# A sync that dies mid-flight (process restart, container kill, power loss)
# used to leave ``inventory_sync.in_progress`` at "1" forever, permanently
# blocking every later sync with 409 and requiring manual DB surgery. A guard
# older than this window is treated as abandoned so the next request recovers.
# The per-record 领星 push guards (采购单 / 供应收货) follow the same reasoning with
# ``PUSH_GUARD_TIMEOUT`` in ``lingxing_writes.py``.
INVENTORY_SYNC_GUARD_TIMEOUT = timedelta(minutes=15)


def upsert_app_setting(
    database: sqlite3.Connection,
    key: str,
    value: object,
    timestamp: str,
) -> None:
    database.execute(
        """
        INSERT INTO app_settings(
            setting_key, setting_value, is_secret, updated_by,
            updated_by_user_id, updated_at
        ) VALUES (?, ?, 0, ?, ?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET
            setting_value = excluded.setting_value,
            updated_by = excluded.updated_by,
            updated_by_user_id = excluded.updated_by_user_id,
            updated_at = excluded.updated_at
        """,
        (
            key,
            str(value),
            current_actor_name("系统管理员"),
            current_actor_id(),
            timestamp,
        ),
    )


def inventory_sync_settings(database: sqlite3.Connection) -> dict[str, str]:
    rows = database.execute(
        "SELECT setting_key, setting_value FROM app_settings "
        "WHERE setting_key LIKE 'inventory_sync.%'"
    ).fetchall()
    return {row["setting_key"].split(".", 1)[1]: row["setting_value"] for row in rows}


def inventory_sync_items(database: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-product stock lines joined to their last Lingxing sync status.

    A product with no ``product_stock_sync`` row has never been synced, so it
    reports ``PENDING`` (design: inventory sync shows the per-product state so
    operations can push a selected subset or everything).
    """
    rows = database.execute(
        """
        SELECT ps.product_model_id, ps.on_hand, ps.updated_at,
               pm.model_code, pm.name,
               pss.sync_status, pss.synced_quantity, pss.lingxing_id,
               pss.push_error, pss.synced_at
        FROM product_stock ps
        JOIN product_models pm ON pm.id = ps.product_model_id
        LEFT JOIN product_stock_sync pss
            ON pss.product_model_id = ps.product_model_id
        ORDER BY pm.model_code
        """
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "productModelId": row["product_model_id"],
                "sku": row["model_code"],
                "productName": row["name"],
                "quantity": row["on_hand"],
                "stockUpdatedAt": row["updated_at"],
                "syncStatus": row["sync_status"] or "PENDING",
                "syncedQuantity": row["synced_quantity"] if row["synced_quantity"] is not None else 0,
                "lingxingId": row["lingxing_id"] or None,
                "pushError": row["push_error"] or "",
                "syncedAt": row["synced_at"],
            }
        )
    return items


def inventory_sync_overall(
    database: sqlite3.Connection, items: list[dict[str, Any]]
) -> dict[str, Any]:
    settings = inventory_sync_settings(database)
    return {
        "syncStatus": settings.get("status") or "PENDING",
        "syncedAt": settings.get("synced_at") or None,
        "lingxingId": settings.get("lingxing_id") or None,
        "error": settings.get("error") or "",
        "inProgress": settings.get("in_progress") == "1",
        "total": len(items),
        "pushed": sum(1 for item in items if item["syncStatus"] == "PUSHED"),
        "pending": sum(1 for item in items if item["syncStatus"] == "PENDING"),
        "failed": sum(1 for item in items if item["syncStatus"] == "FAILED"),
    }


def mark_product_stock_synced(
    database: sqlite3.Connection,
    product_model_id: int,
    synced_quantity: int,
    lingxing_id: str,
    timestamp: str,
) -> None:
    database.execute(
        """
        INSERT INTO product_stock_sync(
            product_model_id, sync_status, synced_quantity, lingxing_id,
            push_error, synced_at
        ) VALUES (?, 'PUSHED', ?, ?, '', ?)
        ON CONFLICT(product_model_id) DO UPDATE SET
            sync_status = 'PUSHED',
            synced_quantity = excluded.synced_quantity,
            lingxing_id = excluded.lingxing_id,
            push_error = '',
            synced_at = excluded.synced_at
        """,
        (product_model_id, synced_quantity, lingxing_id, timestamp),
    )


def mark_product_stock_sync_failed(
    database: sqlite3.Connection,
    product_model_id: int,
    message: str,
    timestamp: str,
) -> None:
    # Preserve the last successful synced_quantity / lingxing_id on conflict;
    # only the status, error and time are refreshed for a failed push.
    database.execute(
        """
        INSERT INTO product_stock_sync(
            product_model_id, sync_status, synced_quantity, lingxing_id,
            push_error, synced_at
        ) VALUES (?, 'FAILED', 0, '', ?, ?)
        ON CONFLICT(product_model_id) DO UPDATE SET
            sync_status = 'FAILED',
            push_error = excluded.push_error,
            synced_at = excluded.synced_at
        """,
        (product_model_id, message, timestamp),
    )


def fetch_pending_receipts_by_sku(
    service: Any, scope_skus: set[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Resolve pending Lingxing 收货单 (采购收货单) grouped by product SKU.

    Calls 查询收货单列表 (getOrderList) for 待收货 purchase 收货单 and returns a
    map of ``SKU -> [{"order_sn", "item_id", "good_num"}]`` so 库存同步 can
    fast-receive each product's line. ``good_num`` is the still-outstanding
    quantity (通知收货量 − 已收货量). ``getOrderList`` cannot filter by SKU, so
    when ``scope_skus`` is given only those SKUs are kept and pagination stops
    as soon as every scoped SKU is matched — a subset sync no longer scans all
    pending 收货单. Paginates up to a safe cap otherwise.
    """
    by_sku: dict[str, list[dict[str, Any]]] = defaultdict(list)
    offset = 0
    length = 200
    for _ in range(10):  # cap at 2000 收货单 rows per sync
        response = service.push(
            "receipt_list",
            {
                "date_type": 3,
                "order_type": 1,
                "status": 10,
                "offset": offset,
                "length": length,
            },
        )
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        rows = data.get("list") or []
        for order in rows:
            order_sn = str(order.get("order_sn") or "").strip()
            if not order_sn:
                continue
            for item in order.get("item_list") or []:
                sku = str(item.get("sku") or "").strip()
                if not sku:
                    continue
                sku_upper = sku.upper()
                if scope_skus is not None and sku_upper not in scope_skus:
                    continue
                try:
                    item_id = int(item.get("item_id"))
                except (TypeError, ValueError):
                    continue
                try:
                    notice = int(float(item.get("notice_num_total") or 0))
                    received = int(float(item.get("product_receive_num") or 0))
                except (TypeError, ValueError):
                    notice, received = 0, 0
                good_num = notice - received
                if good_num <= 0:
                    continue
                by_sku[sku_upper].append(
                    {"order_sn": order_sn, "item_id": item_id, "good_num": good_num}
                )
        # Stop early once every scoped SKU has been resolved.
        if scope_skus is not None and scope_skus.issubset(by_sku.keys()):
            break
        total = int(data.get("total") or 0)
        offset += length
        if offset >= total or not rows:
            break
    return by_sku


def fast_receive_product(
    service: Any,
    model_code: str,
    by_sku: dict[str, list[dict[str, Any]]],
) -> tuple[list[str], int]:
    """Fast-receive every pending 收货单 line for one product's SKU.

    Groups the matched lines by 收货单号 and calls 快捷入库 (fastReceive) once
    per 收货单 with its ``item_list``. Returns the received 收货单号 list and the
    total 良品量. Raises ``ApiError`` when no pending 收货单 matches the SKU.
    """
    lines = by_sku.get(str(model_code or "").upper(), [])
    if not lines:
        raise ApiError("未找到该产品待收货的领星收货单", 404)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        grouped[line["order_sn"]].append(line)
    order_sns: list[str] = []
    good_total = 0
    for order_sn, items in grouped.items():
        service.push(
            "inventory_sync",
            {
                "order_sn": order_sn,
                "item_list": [
                    {
                        "id": item["item_id"],
                        "product_good_num": item["good_num"],
                        "product_bad_num": 0,
                    }
                    for item in items
                ],
            },
        )
        order_sns.append(order_sn)
        good_total += sum(item["good_num"] for item in items)
    return order_sns, good_total
