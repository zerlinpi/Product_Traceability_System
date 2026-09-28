"""The quality-release gate.

Whether a batch may enter finished-goods stock is a quality decision, not a
warehouse one: a 暂扣 (HOLD) batch must never be stocked in, and when the
质量放行 setting is on the batch has to be registered and released first.

Extracted from ``app.py`` because two domains need the same answer — the
production-order routes and scan-gun inbound — and a rule that decides whether
physical stock moves must not exist in two copies.
"""

from __future__ import annotations

import sqlite3

from traceability.validators import parse_bool

__all__ = ["require_quality_release", "stock_in_block_reason"]


def require_quality_release(database: sqlite3.Connection) -> bool:
    row = database.execute(
        "SELECT setting_value FROM system_settings WHERE setting_key = 'require_quality_release'"
    ).fetchone()
    return parse_bool(row["setting_value"] if row else "0")


def stock_in_block_reason(quality_status: str | None, gate: bool) -> str | None:
    """Why 扫码枪入库 must be refused for a batch, or ``None`` when allowed.

    Closes the gap where the 批次质量处理 step had no effect on the flow: a
    暂扣 (HOLD) batch must never enter finished-goods stock. When the
    质量放行 setting is on, the batch additionally has to be registered and
    released (PASSED) first — the same intent the legacy per-unit flow had.
    ``quality_status`` is ``None`` for a batch that was never registered.
    """
    if quality_status == "HOLD":
        return "该批次已暂扣，解除暂扣后才能入库"
    if gate:
        if quality_status is None:
            return "已开启质量放行：请先完成批次登记并放行后再入库"
        if quality_status != "PASSED":
            return "已开启质量放行：该批次需质量合格放行后才能入库"
    return None
