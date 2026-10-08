"""Bounded result windows for the list endpoints.

Why this exists
---------------
Most list endpoints in this application already cap what they return — 500 rows
for machines, part labels and trace records, 300 for generated code sets. A
handful did not, and they were the ones whose tables grow without bound:

    GET /api/batch-trace-records       one row per registration, forever
    GET /api/purchase-orders           one row per order
    GET /api/production-orders         one row per order
    GET /api/inbound-receipts          one row per receipt
    GET /api/supplier-inventory-batches  one row per incoming batch

Measured against a database a few years old (30,000 registrations, 2,000 orders
each), /api/batch-trace-records returned 10.1 MB in one response. The front end
renders those into a plain table with no virtualisation, so the browser was being
asked to lay out 30,000 rows.

This module makes the convention explicit rather than leaving it as a number
copied between files: one default, one ceiling, and a total so a caller can tell
it is looking at a window rather than the whole table.

What callers get
----------------
The response body keeps its existing shape — an array in the usual envelope — so
nothing that already consumes these endpoints breaks. The count of matching rows
travels in headers:

    X-Total-Count   how many rows match the filters, before the window
    X-Returned-Count  how many are in this response
    X-List-Limit    the window size that was applied

A caller that ignores the headers behaves exactly as before; a caller that reads
them can say "showing the most recent 500 of 30,000" instead of pretending the
window is the whole table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import Response, request

from traceability.errors import ApiError

#: Rows returned when the caller does not ask for a specific number. Matches the
#: limit the list endpoints already used before this module existed.
DEFAULT_LIST_LIMIT = 500

#: Ceiling for an explicit ``limit``. A caller may ask for more than the default
#: — exports and drill-downs legitimately do — but not without bound, because the
#: cost of an unbounded response lands on the workstation, not on the server.
MAX_LIST_LIMIT = 5000


@dataclass(frozen=True)
class ListWindow:
    """How much of a result set to return, and from where."""

    limit: int
    offset: int

    def apply(self, payload: tuple[Response, int], total: int) -> tuple[Response, int]:
        """Attach the counts that make a truncated response honest.

        Takes what ``success`` returned — a ``(response, status)`` tuple — and
        gives the same shape back, so an endpoint keeps writing its envelope the
        way every other endpoint does.
        """
        response, status = payload
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Returned-Count"] = str(min(self.limit, max(total - self.offset, 0)))
        response.headers["X-List-Limit"] = str(self.limit)
        return response, status


def _parse_int(raw: Any, label: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise ApiError(f"{label}必须是整数") from error
    if value < minimum or value > maximum:
        raise ApiError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return value


def parse_list_window(
    *,
    default: int = DEFAULT_LIST_LIMIT,
    maximum: int = MAX_LIST_LIMIT,
) -> ListWindow:
    """Read ``limit``/``offset`` from the query string, with safe bounds.

    A caller that passes neither gets ``default`` rows from the start, which is
    what the list endpoints did before — except that they returned everything.
    An invalid value is rejected rather than silently coerced, so a typo in a
    filter does not quietly change which rows come back.
    """
    raw_limit = request.args.get("limit")
    raw_offset = request.args.get("offset")

    limit = default if raw_limit in (None, "") else _parse_int(raw_limit, "每页数量", minimum=1, maximum=maximum)
    offset = 0 if raw_offset in (None, "") else _parse_int(raw_offset, "起始位置", minimum=0, maximum=10_000_000)
    return ListWindow(limit=limit, offset=offset)
