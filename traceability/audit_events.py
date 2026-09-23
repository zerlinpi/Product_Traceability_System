"""The audit ledger's write path.

Extracted from ``app.py`` so a blueprint can record an event. Every state change
in this system writes one of these, which is why it is a module of its own rather
than a helper on the routes that happen to use it.

The chain is computed from the values *about to be written*, so a row goes in
complete in a single statement — see ``traceability/audit_chain.py`` for why the
link is keyed on ``event_id`` rather than the autoincrement ``id``.

The retry loop exists because ``event_id`` is random and the table is
append-only: a collision is astronomically unlikely but not impossible, and
retrying is cheaper than serialising every writer behind a lock.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from traceability.audit_chain import link_event
from traceability.auth import current_actor_id, current_actor_name
from traceability.codes import new_event_id
from traceability.validators import now_iso

__all__ = ["record_audit_event"]


def record_audit_event(
    database: sqlite3.Connection,
    event_type: str,
    object_type: str,
    object_code: str,
    *,
    related_object_code: str = "",
    station_id: str = "",
    station_name: str = "",
    operator_name: str = "",
    reason: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> None:
    actor_user_id = current_actor_id()
    if actor_user_id is not None:
        operator_name = current_actor_name(operator_name)
    payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    timestamp = occurred_at or now_iso()
    for attempt in range(8):
        try:
            event_id = new_event_id()
            # The chain is computed from the values about to be written, so the
            # row can be inserted complete in one statement. See
            # traceability/audit_chain.py for why event_id and not id.
            prev_hash, event_hash = link_event(
                database,
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "object_type": object_type,
                    "object_code": object_code,
                    "related_object_code": related_object_code,
                    "station_id": station_id,
                    "station_name": station_name,
                    "operator_name": operator_name,
                    "actor_user_id": actor_user_id,
                    "reason": reason,
                    "payload_json": payload_json,
                    "occurred_at": timestamp,
                },
            )
            database.execute(
                """
                INSERT INTO audit_events(
                    event_id, event_type, object_type, object_code, related_object_code,
                    station_id, station_name, operator_name, actor_user_id,
                    reason, payload_json, occurred_at, prev_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    object_type,
                    object_code,
                    related_object_code,
                    station_id,
                    station_name,
                    operator_name,
                    actor_user_id,
                    reason,
                    payload_json,
                    timestamp,
                    prev_hash,
                    event_hash,
                ),
            )
            return
        except sqlite3.IntegrityError as error:
            if "audit_events.event_id" not in str(error) or attempt == 7:
                raise
