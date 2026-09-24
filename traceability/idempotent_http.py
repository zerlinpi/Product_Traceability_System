"""The HTTP half of request idempotency.

``traceability/idempotency.py`` holds the storage primitives and deliberately
imports no Flask; this module is the part that reads the request and writes the
response, so the two are kept apart. It lives outside ``create_app`` because a
blueprint cannot reach a nested function.

Field-facing writes are wrapped in ``run_idempotent`` so a scan gun or a flaky LAN
cannot credit finished-goods stock twice when the client retries after a timeout.
The key is claimed *before* the business logic runs, so two concurrent duplicates
serialise on the SQLite write lock instead of both executing.

Requests without a key behave exactly as before — the mechanism is opt-in, so
existing clients keep working.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from flask import current_app, jsonify, request

from traceability.auth import current_actor_id
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.idempotency import (
    CLAIMED,
    CONFLICT,
    IDEMPOTENCY_BODY_FIELD,
    IDEMPOTENCY_HEADER,
    IN_PROGRESS,
    REPLAY,
    IdempotencyError,
    claim,
    complete,
    fingerprint,
    normalize_key,
    release,
)

__all__ = ["run_idempotent"]


def run_idempotent(scope: str, producer: Callable[[], Any]) -> Any:
    """Run ``producer`` at most once per (Idempotency-Key, scope).

    Field-facing writes are wrapped in this so a scan gun or a flaky LAN
    cannot credit finished-goods stock twice when the client retries after a
    timeout. The key is **claimed before** the business logic runs, so two
    concurrent duplicates serialise on the SQLite write lock instead of both
    executing. See ``traceability/idempotency.py``.

    Requests without a key behave exactly as before — the mechanism is
    opt-in, so existing clients keep working.
    """
    payload = request.get_json(silent=True) or {}
    raw_key = request.headers.get(IDEMPOTENCY_HEADER) or payload.get(IDEMPOTENCY_BODY_FIELD)
    if not raw_key:
        return producer()
    try:
        key = normalize_key(raw_key)
    except IdempotencyError as error:
        raise ApiError(str(error)) from error

    database = get_db()
    request_fingerprint = fingerprint(payload)
    outcome, existing = claim(
        database,
        key=key,
        scope=scope,
        request_fingerprint=request_fingerprint,
        actor_user_id=current_actor_id(),
        started_at=current_app.config["NOW_PROVIDER"](),
    )
    if outcome == CONFLICT:
        raise ApiError("幂等键已被另一份不同的请求使用，请重新提交", 409)
    if outcome == IN_PROGRESS:
        raise ApiError("同一请求正在处理中，请勿重复提交", 409)
    if outcome == REPLAY:
        # The original request already committed; hand back its response
        # verbatim so the client sees a deterministic result.
        return jsonify(json.loads(existing["response_json"])), int(existing["status_code"])
    assert outcome == CLAIMED

    try:
        result = producer()
    except Exception:
        # The business transaction rolled back, so nothing happened. Free the
        # key so the client's retry is not stranded.
        release(database, key=key, scope=scope)
        raise

    # Handlers here return either a Response or the ``(Response, status)``
    # tuple that ``success()`` produces; normalise before inspecting it.
    if isinstance(result, tuple):
        response, status_code = result[0], int(result[1])
    else:
        response, status_code = result, int(result.status_code)
    body = response.get_json(silent=True)
    if 200 <= status_code < 300 and body is not None:
        try:
            complete(
                database,
                key=key,
                scope=scope,
                status_code=status_code,
                body=body,
                completed_at=current_app.config["NOW_PROVIDER"](),
            )
        except Exception:
            # The business operation already committed. Failing to record the
            # response must not turn a success into an error; drop the claim
            # so a retry re-runs rather than replaying nothing.
            release(database, key=key, scope=scope)
    else:
        # The business rejected the request, so nothing happened. Let the
        # client retry with the same key.
        release(database, key=key, scope=scope)
    return result
