"""Idempotency for field-facing write endpoints.

The problem this solves
-----------------------
A scan gun or a flaky LAN connection can deliver the same submission twice: the
client times out, the operator retries, and the server has already committed.
For ``POST /api/scan-gun/inbound`` that means finished-goods stock is credited
twice, silently. UI-level double-click guards do not help — the duplicate
arrives as a separate HTTP request.

The mechanism
-------------
The client sends an ``Idempotency-Key`` header (or an ``idempotencyKey`` body
field). The first request claims the key, runs, and stores its response. A later
request with the same key and the same request fingerprint gets the stored
response replayed verbatim instead of running again.

Claim-then-complete, not check-then-act
---------------------------------------
A naive "look up the key, then execute, then store" has a race: two concurrent
duplicates both miss the lookup and both execute. So the key row is **inserted
first**, in its own ``BEGIN IMMEDIATE`` transaction, before the business logic
runs. The insert is the mutual exclusion. A duplicate therefore either replays a
finished result or is told the original is still in flight — it never runs twice.

This mirrors the guard the Lingxing push already uses (``push_in_progress`` plus
``push_started_at``), so there is one recovery story rather than two:
``clear_in_progress`` runs at startup alongside ``_clear_abandoned_guards``,
because a claim can only be held by a live request.

This module deliberately has **no Flask dependency**, matching
``traceability/capabilities.py``, so it can be tested and reasoned about
directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_BODY_FIELD = "idempotencyKey"

MAX_KEY_LENGTH = 128
MIN_KEY_LENGTH = 8

# How long a completed key is remembered. Long enough to cover any realistic
# retry window on a factory LAN (an operator noticing a timeout and resubmitting
# is a matter of minutes, not days); short enough that the table stays small.
RETENTION_SECONDS = 7 * 24 * 60 * 60

# Outcome of an attempt to claim a key.
CLAIMED = "claimed"
REPLAY = "replay"
IN_PROGRESS = "in_progress"
CONFLICT = "conflict"


class IdempotencyError(ValueError):
    """The supplied key is unusable."""


def normalize_key(raw: object) -> str:
    """Validate and normalise a client-supplied idempotency key.

    Keys are opaque to the server, but a bound on length keeps a hostile or
    buggy client from writing megabyte rows.
    """
    if raw is None:
        raise IdempotencyError("缺少幂等键")
    key = str(raw).strip()
    if not MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH:
        raise IdempotencyError(f"幂等键长度需为 {MIN_KEY_LENGTH}-{MAX_KEY_LENGTH} 个字符")
    if not all(character.isprintable() for character in key):
        raise IdempotencyError("幂等键不能包含不可打印字符")
    return key


def fingerprint(payload: Any) -> str:
    """Stable hash of a request payload.

    Reusing a key with a *different* payload is a client bug, and silently
    replaying the first response would hide it. The fingerprint lets the server
    detect that and answer 409 instead.
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup(connection: sqlite3.Connection, key: str, scope: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM idempotency_keys WHERE idempotency_key = ? AND scope = ?",
        (key, scope),
    ).fetchone()


def claim(
    connection: sqlite3.Connection,
    *,
    key: str,
    scope: str,
    request_fingerprint: str,
    actor_user_id: int | None,
    started_at: str,
) -> tuple[str, sqlite3.Row | None]:
    """Try to take ownership of ``key`` for ``scope``.

    Returns ``(outcome, row)``. The row is present for REPLAY / IN_PROGRESS /
    CONFLICT so the caller can decide what to answer.

    The insert happens inside ``BEGIN IMMEDIATE`` so that concurrent duplicates
    serialise on the SQLite write lock and exactly one of them claims the key.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO idempotency_keys(
                idempotency_key, scope, request_fingerprint, state,
                status_code, response_json, actor_user_id, started_at, completed_at
            ) VALUES (?, ?, ?, 'in_progress', NULL, NULL, ?, ?, NULL)
            """,
            (key, scope, request_fingerprint, actor_user_id, started_at),
        )
        connection.execute("COMMIT")
        return CLAIMED, None
    except sqlite3.IntegrityError:
        connection.execute("ROLLBACK")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    existing = lookup(connection, key, scope)
    if existing is None:  # pragma: no cover - the row vanished between statements
        raise IdempotencyError("幂等键状态异常，请重试")
    if existing["request_fingerprint"] != request_fingerprint:
        return CONFLICT, existing
    if existing["state"] == "completed":
        return REPLAY, existing
    return IN_PROGRESS, existing


def complete(
    connection: sqlite3.Connection,
    *,
    key: str,
    scope: str,
    status_code: int,
    body: Any,
    completed_at: str,
) -> None:
    """Attach the finished response to a claimed key."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE idempotency_keys
            SET state = 'completed', status_code = ?, response_json = ?, completed_at = ?
            WHERE idempotency_key = ? AND scope = ?
            """,
            (
                status_code,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                completed_at,
                key,
                scope,
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def release(connection: sqlite3.Connection, *, key: str, scope: str) -> None:
    """Drop a claim whose business operation failed.

    The operation rolled back, so nothing happened; letting the client retry
    with the same key is the correct outcome rather than stranding it.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "DELETE FROM idempotency_keys "
            "WHERE idempotency_key = ? AND scope = ? AND state = 'in_progress'",
            (key, scope),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def clear_in_progress(connection: sqlite3.Connection) -> int:
    """Delete claims left behind by a process that died mid-request.

    Called at startup next to ``_clear_abandoned_guards``. No request can be in
    flight while the database is being initialised, so every remaining claim
    belongs to a process that never finished.
    """
    cursor = connection.execute(
        "DELETE FROM idempotency_keys WHERE state = 'in_progress'"
    )
    return cursor.rowcount


def purge_expired(
    connection: sqlite3.Connection, *, now_epoch: int, retention_seconds: int = RETENTION_SECONDS
) -> int:
    """Drop completed keys older than the retention window (bounded retention)."""
    cutoff = now_epoch - retention_seconds
    cursor = connection.execute(
        "DELETE FROM idempotency_keys "
        "WHERE state = 'completed' AND CAST(strftime('%s', completed_at) AS INTEGER) < ?",
        (cutoff,),
    )
    return cursor.rowcount
