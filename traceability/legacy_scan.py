"""The legacy per-unit (逐台) scan flow: its retirement rule and station state.

Before batch traceability, a finished product was traced one unit at a time:
per-unit QR sets were generated for a product (``/api/products/<id>/code-sets``)
and a station scanned the main code and then each part code in BOM order
(``/api/scan``), producing one ``trace_records`` row per unit.

Treadmill (走步机) products moved to batch-level traceability, so both entry
points refuse them (``is_treadmill_product_model`` with
``LEGACY_ENTRY_DISABLED_MESSAGE``); every other product family keeps the flow.
The rule is shared by the code-set routes and the scan-station routes, which is
why it lives here rather than in either blueprint. The station's in-progress
session (``ensure_station_access``, ``session_state``) belongs to the scan
routes and sits next to it for the same reason: it is part of this flow.

Extracted from ``app.py`` when those routes moved to ``traceability/api/``.
``app.py`` still re-exports ``LEGACY_ENTRY_DISABLED_MESSAGE`` because tests
import it from there.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from traceability.auth import current_actor_id
from traceability.errors import ApiError
from traceability.serializers import machine_dict, part_label_dict
from traceability.trace_plans import get_plan_summary

__all__ = [
    "DEFAULT_GENERIC_PART_COUNT",
    "LEGACY_ENTRY_DISABLED_MESSAGE",
    "SESSION_TIMEOUT_HOURS",
    "TREADMILL_FAMILY_CODE",
    "ensure_station_access",
    "is_treadmill_product_model",
    "session_state",
]


# Fixed number of components expected by the legacy per-unit (逐台) generic scan
# flow when a product has no BOM/expected slots. Formerly configurable via the
# ``required_part_count`` system setting, which has been removed.
DEFAULT_GENERIC_PART_COUNT = 2
SESSION_TIMEOUT_HOURS = 8

# Requirement 7.3: treadmill (走步机) products moved to batch-level traceability,
# so their legacy per-unit main-code + per-component assembly entry endpoints are
# retired. Direct calls to those entry points are rejected with this message and
# an HTTP 409, without creating any per-unit traceability record. Non-treadmill
# products keep their previous behavior unchanged (Requirement 7.5), and historical
# ``machines`` / ``trace_records`` / ``product_code_sets`` rows stay read-only
# queryable (Requirement 7.4).
TREADMILL_FAMILY_CODE = "TREADMILL"
LEGACY_ENTRY_DISABLED_MESSAGE = "走步机产品已改为按批次管理，逐台/逐部件录入入口已停用"


def is_treadmill_product_model(
    database: sqlite3.Connection, product_model_id: int | None
) -> bool:
    """Return True when ``product_model_id`` belongs to the treadmill family.

    The treadmill product family (``product_families.product_code = 'TREADMILL'``)
    is the one whose per-unit / per-component entry has been retired in favor of
    batch traceability (Requirement 7.3). Any other family (bed frames, custom
    products, legacy imports, ...) is left untouched (Requirement 7.5). A missing
    or unknown product model resolves to ``False`` so callers fall through to
    their normal not-found handling instead of masking it as a disabled entry.
    """
    if product_model_id is None:
        return False
    row = database.execute(
        """
        SELECT pf.product_code AS family_code
        FROM product_models pm
        JOIN product_families pf ON pf.id = pm.product_family_id
        WHERE pm.id = ?
        """,
        (product_model_id,),
    ).fetchone()
    if row is None:
        return False
    return (row["family_code"] or "").strip().upper() == TREADMILL_FAMILY_CODE


def ensure_station_access(database: sqlite3.Connection, station_id: str) -> None:
    actor_user_id = current_actor_id()
    if actor_user_id is None:
        return
    owner = database.execute(
        "SELECT user_id, operator_name, machine_id FROM scan_sessions WHERE station_id = ?",
        (station_id,),
    ).fetchone()
    if owner and owner["machine_id"] and owner["user_id"] not in {None, actor_user_id}:
        raise ApiError(f"该扫码工位正由“{owner['operator_name']}”使用", 409)


def session_state(database: sqlite3.Connection, station_id: str) -> dict[str, Any]:
    session = database.execute(
        "SELECT * FROM scan_sessions WHERE station_id = ?", (station_id,)
    ).fetchone()
    machine = None
    items: list[dict[str, Any]] = []
    plan = None
    expected_slots: list[dict[str, Any]] = []
    if session and session["machine_id"]:
        machine_row = database.execute(
            """
            SELECT m.*, 0 AS traced, NULL AS reserved_station,
                   pm.name AS product_model_name,
                   pf.id AS product_family_id, pf.product_code AS product_family_code,
                   pf.name AS product_family_name,
                   tp.version AS trace_plan_version, tp.name AS trace_plan_name
            FROM machines m
            LEFT JOIN product_models pm ON pm.id = m.product_model_id
            LEFT JOIN product_families pf ON pf.id = pm.product_family_id
            LEFT JOIN trace_plans tp ON tp.id = m.trace_plan_id
            WHERE m.id = ?
            """,
            (session["machine_id"],),
        ).fetchone()
        if machine_row:
            machine = machine_dict(machine_row)
        plan_id = session["trace_plan_id"] or (machine_row["trace_plan_id"] if machine_row else None)
        plan = get_plan_summary(database, plan_id)
        expected_slots = plan["slots"] if plan else []
        item_rows = database.execute(
            """
            SELECT ssi.position, pl.*, pt.part_code, pt.name AS part_name,
                   pt.category_code, pt.category_name,
                   s.supplier_code, s.name AS supplier_name,
                   0 AS used, NULL AS reserved_station
            FROM scan_session_items ssi
            JOIN part_labels pl ON pl.id = ssi.part_label_id
            JOIN part_types pt ON pt.id = pl.part_type_id
            JOIN suppliers s ON s.id = pt.supplier_id
            WHERE ssi.station_id = ?
            ORDER BY ssi.position
            """,
            (station_id,),
        ).fetchall()
        items = []
        for row in item_rows:
            item = {"position": row["position"], **part_label_dict(row)}
            if expected_slots and row["position"] <= len(expected_slots):
                item["slot"] = expected_slots[row["position"] - 1]
            items.append(item)

    required_count = len(expected_slots) if expected_slots else DEFAULT_GENERIC_PART_COUNT
    part_count = len(items)
    if not machine:
        next_expected = "machine"
        current_step = 0
    elif part_count < required_count:
        next_expected = "part"
        current_step = 1 + part_count
    else:
        next_expected = "complete"
        current_step = 1 + required_count

    return {
        "stationId": station_id,
        "stationName": session["station_name"] if session else "",
        "operatorName": session["operator_name"] if session else "",
        "requiredPartCount": required_count,
        "workflowMode": "BOM" if plan else "GENERIC",
        "tracePlan": plan,
        "expectedSlots": expected_slots,
        "nextExpectedSlot": expected_slots[part_count] if expected_slots and part_count < len(expected_slots) else None,
        "totalSteps": 1 + required_count,
        "currentStep": current_step,
        "nextExpected": next_expected,
        "machine": machine,
        "parts": items,
        "updatedAt": session["updated_at"] if session else None,
    }
