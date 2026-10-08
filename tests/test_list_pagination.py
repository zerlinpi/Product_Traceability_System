"""List endpoints return a bounded window, and say so.

Why this file exists
--------------------
Most list endpoints already capped what they returned; five did not, and they were
the ones whose tables grow without bound. Measured against a database a few years
old, /api/batch-trace-records sent 10.1 MB in a single response for 30,000 rows —
into a front end that renders every row it is given, without virtualisation.

The response body deliberately keeps its existing shape, so nothing that already
consumes these endpoints breaks. The counts travel in headers instead:

    X-Total-Count      rows matching the filters, before the window
    X-Returned-Count   rows in this response
    X-List-Limit       the window that was applied

The tests below hold both halves of that bargain: the window is real, and the
envelope every existing caller parses is untouched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capability_helpers import bootstrap_admin, make_auth_app  # noqa: E402
from traceability.errors import ApiError  # noqa: E402
from traceability.pagination import (  # noqa: E402
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ListWindow,
    parse_list_window,
)


# --------------------------------------------------------------------------
# The window itself
# --------------------------------------------------------------------------


def window_for(query: str) -> ListWindow:
    """Parse a query string the way a request would."""
    app = Flask(__name__)
    with app.test_request_context(f"/?{query}"):
        return parse_list_window()


def test_no_arguments_means_the_default_window():
    window = window_for("")

    assert window.limit == DEFAULT_LIST_LIMIT
    assert window.offset == 0


def test_an_explicit_limit_and_offset_are_honoured():
    window = window_for("limit=25&offset=50")

    assert window.limit == 25
    assert window.offset == 50


def test_an_empty_limit_falls_back_to_the_default():
    """A form that submits an empty field must not become an error."""
    window = window_for("limit=&offset=")

    assert window.limit == DEFAULT_LIST_LIMIT
    assert window.offset == 0


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "  "])
def test_a_nonsensical_limit_is_rejected_with_a_chinese_message(value):
    with pytest.raises(ApiError) as error:
        window_for(f"limit={value}")

    assert "每页数量" in str(error.value)


def test_a_limit_above_the_ceiling_is_rejected():
    with pytest.raises(ApiError) as error:
        window_for(f"limit={MAX_LIST_LIMIT + 1}")

    assert "每页数量" in str(error.value)


def test_a_negative_offset_is_rejected():
    with pytest.raises(ApiError) as error:
        window_for("offset=-1")

    assert "起始位置" in str(error.value)


def test_the_headers_report_the_window_honestly():
    """A caller must be able to tell a window from the whole table."""
    window = ListWindow(limit=100, offset=200)

    response, _status = window.apply(_envelope([]), total=1_000)

    assert response.headers["X-Total-Count"] == "1000"
    assert response.headers["X-Returned-Count"] == "100"
    assert response.headers["X-List-Limit"] == "100"


def test_the_returned_count_shrinks_at_the_end_of_the_data():
    """The last page is short; saying otherwise would mislead a paging loop."""
    window = ListWindow(limit=100, offset=950)

    response, _status = window.apply(_envelope([]), total=1_000)

    assert response.headers["X-Returned-Count"] == "50"


def test_the_returned_count_never_goes_negative():
    window = ListWindow(limit=100, offset=5_000)

    response, _status = window.apply(_envelope([]), total=1_000)

    assert response.headers["X-Returned-Count"] == "0"


def _envelope(rows):
    """A stand-in for what ``success`` returns, without needing an app context.

    The header tests are about the counts, not the envelope, so they should not
    drag in Flask's application context to build one.
    """
    from flask import Response

    return Response(json.dumps({"ok": True, "data": rows}), mimetype="application/json"), 200


# --------------------------------------------------------------------------
# The endpoints, against a database with more rows than one window
# --------------------------------------------------------------------------


@pytest.fixture()
def busy(tmp_path):
    """A migrated database with 600 registrations across 600 batches."""
    app, database_path, _fake = make_auth_app(tmp_path)
    connection = sqlite3.connect(str(database_path))
    try:
        for index in range(600):
            cursor = connection.execute(
                "INSERT INTO production_batches(batch_code, product_model_id, prefix, "
                "planned_quantity, generated_at) VALUES (?, 1, 'TW', 10, ?)",
                (f"PB-{index:04d}", f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}"),
            )
            connection.execute(
                "INSERT INTO batch_trace_records(production_batch_id, registered_quantity, "
                "registered_at, quality_status) VALUES (?, 10, ?, 'ASSEMBLED')",
                (cursor.lastrowid, "2026-01-01T00:00:00"),
            )
        connection.commit()
    finally:
        connection.close()
    return app


def admin_client(app):
    """A signed-in administrator, using the same helper the other suites use."""
    client, _csrf = bootstrap_admin(app)
    return client


def test_the_endpoint_returns_a_window_not_the_whole_table(busy):
    app = busy
    client = admin_client(app)

    response = client.get("/api/batch-trace-records")

    assert response.status_code == 200
    assert len(response.get_json()["data"]) == DEFAULT_LIST_LIMIT
    assert response.headers["X-Total-Count"] == "600"


def test_the_envelope_shape_is_unchanged(busy):
    """Every existing caller parses this; a window must not reshape it."""
    app = busy
    client = admin_client(app)

    payload = client.get("/api/batch-trace-records").get_json()

    assert set(payload) == {"ok", "data"}
    assert payload["ok"] is True
    assert isinstance(payload["data"], list)


def test_an_explicit_limit_is_honoured_by_the_endpoint(busy):
    app = busy
    client = admin_client(app)

    response = client.get("/api/batch-trace-records?limit=10")

    assert len(response.get_json()["data"]) == 10
    assert response.headers["X-Total-Count"] == "600"


def test_paging_through_covers_every_row_exactly_once(busy):
    """Stable ordering is what makes a paging loop safe.

    The endpoints order by ``(generated_at DESC, id DESC)``; without the id
    tiebreak, rows sharing a timestamp could appear twice or not at all.
    """
    app = busy
    client = admin_client(app)

    seen: list[int] = []
    offset = 0
    while True:
        page = client.get(f"/api/batch-trace-records?limit=250&offset={offset}").get_json()["data"]
        if not page:
            break
        seen.extend(row["id"] for row in page)
        offset += 250
        assert offset <= 1_000, "分页未终止"

    assert len(seen) == 600
    assert len(set(seen)) == 600, "分页出现重复行"


def test_a_rejected_limit_reaches_the_client_as_a_clear_error(busy):
    app = busy
    client = admin_client(app)

    response = client.get("/api/batch-trace-records?limit=0")

    assert response.status_code == 400
    assert "每页数量" in response.get_json()["message"]


def test_the_total_ignores_the_window(busy):
    """The count describes the filter result, not the page."""
    app = busy
    client = admin_client(app)

    first = client.get("/api/batch-trace-records?limit=1&offset=0")
    last = client.get("/api/batch-trace-records?limit=1&offset=599")

    assert first.headers["X-Total-Count"] == last.headers["X-Total-Count"] == "600"


# --------------------------------------------------------------------------
# The export path must not be windowed
# --------------------------------------------------------------------------


def test_the_purchase_order_query_still_returns_everything_without_a_window(tmp_path):
    """A file is not a screen: the export calls the same query with no window.

    If this ever starts returning a window, exported workbooks would silently lose
    orders — the opposite failure from the one this round fixed.
    """
    from traceability.purchasing import query_purchase_orders

    app, _database_path, _fake = make_auth_app(tmp_path)
    with app.app_context():
        from traceability.db import get_db

        database = get_db()
        database.execute(
            "INSERT INTO suppliers(supplier_code, name, created_at, updated_at) "
            "VALUES ('SUP-PG', '分页测试供应商', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        for index in range(600):
            database.execute(
                "INSERT INTO purchase_orders(po_no, supplier_id, sync_status, created_at) "
                "VALUES (?, 1, 'PENDING', '2026-01-01T00:00:00')",
                (f"PO-{index:04d}",),
            )
        database.commit()

        everything = query_purchase_orders()
        windowed = query_purchase_orders(limit=100, offset=0)

    assert len(everything) == 600, "导出路径被意外限制了"
    assert len(windowed) == 100
    assert everything[0]["total_count"] == 600


def test_the_benchmark_tool_is_importable():
    """It documents the numbers this round was decided on; keep it working."""
    sys.path.insert(0, str(ROOT / "tools"))
    import benchmark_lists

    assert benchmark_lists.ROWS["batch_trace_records"] > 0
    assert json.dumps(benchmark_lists.ROWS)  # serialisable, i.e. plain data
