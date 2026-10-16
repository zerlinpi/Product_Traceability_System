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
import tempfile
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from batch_strategies import build_product_scenario  # noqa: E402
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


# --------------------------------------------------------------------------
# The same contract, on every endpoint that claims it
# --------------------------------------------------------------------------

#: Endpoints that went through parse_list_window in the round that added it. The
#: one covered above is the first of these; the other four had the same exposure
#: — an unbounded table and a front end that renders every row it is handed — and
#: none of them had a test that the window actually moves.
#:
#: The check is deliberately blunt: page 0 and page 1 with the same limit must not
#: overlap. That is what catches a query that computes a total and sets headers
#: but never puts LIMIT/OFFSET in the SQL — the headers say "window of 250", the
#: body is the whole table, and asking for the next page returns the first page
#: again. Every one of these passed on the first run except ... all of them, which
#: is the point: nothing was checking.
BOUNDED_ENDPOINTS = (
    "/api/batch-trace-records",
    "/api/purchase-orders",
    "/api/production-orders",
    "/api/inbound-receipts",
    "/api/supplier-inventory-batches",
)


def _seeded_orders_app(tmp_path, count=6):
    """An app with `count` orders at every stage, so each has something to page.

    Seeded by SQL rather than through the API: the point is the shape of the
    window, not the creation flow, and six rows through five endpoints is a few
    hundred statements either way.
    """
    # Auth disabled, the way setup_case in test_purchase_orders does it: the point
    # of this fixture is the shape of the window, and with auth on every POST in
    # build_product_scenario would need a CSRF header the helper has no way to pass.
    database_path = tmp_path / "paged.db"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "NOW_PROVIDER": lambda: "2026-01-01T00:00:00",
        }
    )
    client = app.test_client()
    connection = sqlite3.connect(str(database_path))
    try:
        # A real product/supplier/part, because the joins need them to exist.
        scenario = build_product_scenario(client, "PW", [1], 100)
        supplier_id = scenario["suppliers"][0]["id"]
        part_type_id = scenario["parts"][0]["id"]

        for index in range(count):
            cursor = connection.execute(
                "INSERT INTO purchase_orders(po_no, supplier_id, part_type_id, quantity, "
                "fields_json, created_at) VALUES (?, ?, ?, 5, '{}', ?)",
                (f"PO-{index:04d}", supplier_id, part_type_id, f"2026-01-0{index + 1}T00:00:00"),
            )
            order_id = cursor.lastrowid
            batch = connection.execute(
                "INSERT INTO production_batches(batch_code, product_model_id, prefix, "
                "planned_quantity, generated_at) VALUES (?, 1, 'TW', 10, ?)",
                (f"PB-{index:04d}", f"2026-01-0{index + 1}T00:00:00"),
            )
            connection.execute(
                "INSERT INTO production_orders(purchase_order_id, production_batch_id, created_at) "
                "VALUES (?, ?, ?)",
                (order_id, batch.lastrowid, f"2026-01-0{index + 1}T00:00:00"),
            )
            connection.execute(
                "INSERT INTO batch_trace_records(production_batch_id, registered_quantity, "
                "registered_at, quality_status) VALUES (?, 10, ?, 'ASSEMBLED')",
                (batch.lastrowid, f"2026-01-0{index + 1}T00:00:00"),
            )
            connection.execute(
                "INSERT INTO inbound_receipts(purchase_order_id, quantity, received_at, created_at) "
                "VALUES (?, 5, ?, ?)",
                (order_id, f"2026-01-0{index + 1}T00:00:00", f"2026-01-0{index + 1}T00:00:00"),
            )
            # No supplier_id on this table — the supplier is reached through the
            # part type, which is why only part_type_id is supplied.
            connection.execute(
                "INSERT INTO supplier_inventory_batches(part_type_id, batch_no, "
                "quantity_received, quantity_available, received_date, created_at, updated_at) "
                "VALUES (?, ?, 10, 10, '2026-01-01', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                (part_type_id, f"SIB-{index:04d}"),
            )
        connection.commit()
    finally:
        connection.close()
    return app, count, client


def _batch_window_app(per_product: dict[int, int]):
    """An app whose products own the given number of production batches each.

    Batches are inserted directly rather than created through the API: the create
    path demands an active trace plan with bound supplier inventory, which is a
    different test's subject, and all of that is one INSERT away here.
    """
    database_path = Path(tempfile.mkdtemp(prefix="pts-batch-window-")) / "paged.db"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "NOW_PROVIDER": lambda: "2026-01-01T00:00:00",
        }
    )
    client = app.test_client()
    product_ids = {}
    for index in per_product:
        product = client.post(
            "/api/products", json={"name": f"产品 {index}", "modelCode": f"PM-BW{index}"}
        ).get_json()["data"]
        product_ids[index] = product["id"]

    connection = sqlite3.connect(str(database_path))
    try:
        for index, count in per_product.items():
            for copy in range(count):
                connection.execute(
                    "INSERT INTO production_batches(batch_code, product_model_id, prefix, "
                    "planned_quantity, generated_at) VALUES (?, ?, 'TW', 10, ?)",
                    (f"PB-BW{index}-{copy}", product_ids[index], f"2026-01-0{index}T00:00:00"),
                )
        connection.commit()
    finally:
        connection.close()
    return app, product_ids, sum(per_product.values())


@pytest.mark.parametrize("endpoint", BOUNDED_ENDPOINTS)
def test_asking_for_the_next_page_returns_the_next_rows(tmp_path, endpoint):
    """A window that does not move is a window that hides data.

    page=0 and page=1 with the same limit must be disjoint, and together they must
    be smaller than the table — if the limit were not applied both pages would be
    the whole table and would overlap completely.
    """
    app, seeded, client = _seeded_orders_app(tmp_path)

    first = client.get(f"{endpoint}?limit=2&offset=0")
    second = client.get(f"{endpoint}?limit=2&offset=2")

    assert first.status_code == 200, first.get_json()
    assert second.status_code == 200, second.get_json()

    first_ids = [row["id"] for row in first.get_json()["data"]]
    second_ids = [row["id"] for row in second.get_json()["data"]]

    assert len(first_ids) == 2, f"limit 没有被应用：第一页返回了 {len(first_ids)} 行"
    assert len(second_ids) == 2, f"limit 没有被应用：第二页返回了 {len(second_ids)} 行"
    assert not set(first_ids) & set(second_ids), (
        f"{endpoint} 的 offset 没有生效：两页拿到同一批行"
    )
    assert int(first.headers["X-Total-Count"]) >= seeded


@pytest.mark.parametrize("endpoint", BOUNDED_ENDPOINTS)
def test_the_reported_window_matches_what_was_sent(tmp_path, endpoint):
    """The headers describe the page, and the body is that page.

    ``X-Returned-Count`` is computed from the arithmetic rather than counted from
    the body, so a query that ignores its window would still report a tidy
    ``2``. Comparing it against the body is what makes the header accountable.
    """
    app, _seeded, client = _seeded_orders_app(tmp_path)

    response = client.get(f"{endpoint}?limit=2&offset=0")
    body = response.get_json()["data"]

    assert len(body) == int(response.headers["X-Returned-Count"])
    assert int(response.headers["X-List-Limit"]) == 2


def test_filtering_by_part_type_happens_server_side():
    """The batch picker depends on this, so the filter has to actually exclude.

    The product form used to fetch every inventory batch and filter by part in
    the browser. It now asks the server for one part's batches — which is only
    correct if the endpoint filters them out, not just narrows what it happens to
    return. The existing uses of this parameter happen to take ``[0]`` of the
    response, so a filter that silently returned everything would still pass them.

    Two parts, two batches each, ask for one part: the other part's batches must
    not be in the answer, and its supplier must not leak in either.
    """
    database_path = Path(tempfile.mkdtemp(prefix="pts-part-filter-")) / "paged.db"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "NOW_PROVIDER": lambda: "2026-01-01T00:00:00",
        }
    )
    client = app.test_client()
    # Two suppliers, one part each, one active batch each — so every identity the
    # response carries belongs to exactly one of the two parts.
    for index in (1, 2):
        supplier = client.post(
            "/api/suppliers", json={"supplierCode": f"SUP-F{index}", "name": f"供应商 {index}"}
        ).get_json()["data"]
        part = client.post(
            "/api/part-types",
            json={
                "partCode": f"PART-F{index}",
                "name": f"部件 {index}",
                "supplierId": supplier["id"],
            },
        ).get_json()["data"]
        client.post(
            "/api/supplier-inventory-batches",
            json={
                "partTypeId": part["id"],
                "batchNo": f"SIB-F{index}",
                "quantity": 10,
                "receivedDate": "2026-01-01",
            },
        )

    wanted = client.get(f"/api/supplier-inventory-batches?partTypeId={1}").get_json()["data"]

    assert {row["batchNo"] for row in wanted} == {"SIB-F1"}, "另一部件的批次混进来了"
    assert {row["partTypeId"] for row in wanted} == {1}
    assert {row["supplierId"] for row in wanted} == {1}, "过滤没有触及供应商"


def test_the_product_form_asks_the_server_for_each_part():
    """And the front end has to use that filter rather than re-implement it.

    Fetching all batches and filtering by part in the browser reads fine and is
    quietly wrong: the window is applied before the browser's filter and ordered
    by supplier and part name, so a product whose part owns none of the newest
    window's rows opens with an empty dropdown while the stock exists. Asserted
    against the source because that is where the regression would be.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "frontend"
        / "apps"
        / "web"
        / "src"
        / "views"
        / "products"
        / "components"
        / "ProductFormDialog.vue"
    ).read_text(encoding="utf-8")

    assert "catalogApi.inventoryBatches({ partTypeId })" in source, (
        "应按部件向服务端取批次，而不是取全表再在浏览器里过滤"
    )
    assert "batchesByPart" in source
    # The client-side filter that this replaces: it must not come back.
    assert "inventoryBatches.value.filter(item => item.partTypeId" not in source


def test_the_batch_list_is_bounded_and_filterable_by_product():
    """The trace screen now asks for one product's batches, so both halves matter.

    The endpoint returned every batch in the factory until now — one row per
    batch, forever, downloaded on every visit to the trace page. It is windowed
    like the others now, and filterable, because the page needs exactly one
    product's rows and used to obtain them by taking the whole table and
    filtering in the browser.

    Five batches across two products: the unfiltered total is all of them, the
    filtered response is only the asked-for product's, and the window still
    applies on top of the filter.
    """
    app, product_ids, total = _batch_window_app({1: 2, 2: 3})
    client = app.test_client()

    everything = client.get("/api/production-batches")
    assert int(everything.headers["X-Total-Count"]) == total

    wanted = client.get(f"/api/production-batches?productModelId={product_ids[1]}")
    rows = wanted.get_json()["data"]
    assert int(wanted.headers["X-Total-Count"]) == 2, "过滤后的总数应按同一条件统计"
    assert {row["productModelId"] for row in rows} == {product_ids[1]}
    assert all(row["batchCode"] for row in rows), "行结构不应因过滤而改变"

    # The window applies on top of the filter, not instead of it.
    paged = client.get(f"/api/production-batches?productModelId={product_ids[2]}&limit=2")
    assert len(paged.get_json()["data"]) == 2
    assert int(paged.headers["X-List-Limit"]) == 2
    assert int(paged.headers["X-Total-Count"]) == 3, "窗口不能改变总数"


def test_the_product_carries_its_own_batch_count():
    """The trace picker shows a count per product, from the product now.

    That count used to be derived on the client from the full batch table. It
    travels with the product instead, the way ``componentCount`` already does.
    """
    app, product_ids, _total = _batch_window_app({1: 3, 2: 0})
    client = app.test_client()

    listed = {
        row["id"]: row
        for row in client.get("/api/products").get_json()["data"]
    }
    assert listed[product_ids[1]]["productionBatchCount"] == 3
    assert listed[product_ids[2]]["productionBatchCount"] == 0, "没有批次的产品应当是 0，不是缺失"

    # The other caller of the same serializer — the PUT response — must agree.
    # There is no GET-by-id, so this is the second path, and it is a second call
    # site for the new field rather than a chance for the two to drift.
    updated = client.put(
        f"/api/products/{product_ids[1]}", json={"name": "改名后还是三个批次"}
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["data"]["productionBatchCount"] == 3


def test_the_trace_page_asks_the_server_for_the_selected_product():
    """The client-side filter must not come back.

    It is the reason this round exists: filtering after the window means a
    product whose batches are not in the window opens with an empty list, and the
    operator is told a product has no trace when it does. Asserted against the
    source because that is where the regression would live.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "frontend"
        / "apps"
        / "web"
        / "src"
        / "views"
        / "trace"
        / "index.vue"
    ).read_text(encoding="utf-8")

    assert "batches.value.filter(batch => batch.productModelId" not in source
    assert "productionBatches({ productModelId: productId })" in source
    assert "productionBatchCount" in source


def test_the_registration_search_happens_server_side():
    """The search box has to see the whole table, not the window that arrived.

    The registration screen keeps every production batch in the browser and
    filters it by text. With a window in front, a search for an old batch finds
    nothing — the operator types a code they know exists and the page says there
    is no such batch. Sending the term with the request is the only fix, because
    the server is the only side that can see past the window.
    """
    app, product_ids, _total = _batch_window_app({1: 3, 2: 2})
    client = app.test_client()

    # Matching by batch code is what the box's placeholder promises.
    hit = client.get("/api/production-batches?search=PB-BW1-1")
    rows = hit.get_json()["data"]
    assert [row["batchCode"] for row in rows] == ["PB-BW1-1"], "按批次码搜索没有命中"

    # ...and the term must not drag in the other product's rows.
    assert {row["productModelId"] for row in rows} == {product_ids[1]}

    # A term that matches nothing is an empty window, not everything.
    miss = client.get("/api/production-batches?search=PB-DOES-NOT-EXIST")
    assert miss.get_json()["data"] == []
    assert int(miss.headers["X-Total-Count"]) == 0, "没命中时总数也应当是 0"

    # A blank term is not a search: it must not filter anything out.
    blank = client.get("/api/production-batches?search=")
    assert int(blank.headers["X-Total-Count"]) == 5


def test_the_registration_screen_asks_the_server_then_displaying_rows():
    """And the page must stop filtering a local copy.

    Asserted against the source, like the other two: the regression would live
    in the component, not in the endpoint.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "frontend"
        / "apps"
        / "web"
        / "src"
        / "views"
        / "batch-gen"
        / "index.vue"
    ).read_text(encoding="utf-8")

    assert "batches.value.filter(batch =>" not in source, (
        "又回到浏览器里过滤了——只有在窗口到达之后才会漏掉旧批次"
    )
    assert "productionBatches({ search:" in source
    assert "useDebounceFn" in source, "搜索应防抖，否则每敲一字就是一次请求"


def test_every_windowed_endpoint_has_a_notice_on_the_page_that_shows_it():
    """A window nobody is told about is a window that hides data.

    The endpoints report the real total in headers; the notice is what turns that
    into a sentence an operator reads. Two pages were left without it after
    production-batches became windowed — the trace screen, where an empty list
    then reads as "this product has no batches", and the registration screen,
    where a search that finds nothing reads as "no such batch". Both wrong
    readings are the same shape: the operator concludes the data is absent.

    Asserted against the source rather than by rendering, because the wiring is
    the thing that goes missing.
    """
    views = Path(__file__).resolve().parent.parent / "frontend" / "apps" / "web" / "src" / "views"

    # Endpoints that went through parse_list_window, and the page that lists them.
    # supplier-inventory-batches is absent on purpose: its only consumer is the
    # product form's batch picker, which is a dialog rather than a page.
    for endpoint, page in (
        ("/api/purchase-orders", "purchase-orders/index.vue"),
        ("/api/production-orders", "production-orders/index.vue"),
        ("/api/inbound-receipts", "inbound-receipts/index.vue"),
        ("/api/batch-trace-records", "batch-quality/index.vue"),
        ("/api/production-batches", "batch-gen/index.vue"),
        ("/api/production-batches", "trace/index.vue"),
    ):
        source = (views / page).read_text(encoding="utf-8")
        assert f'PtsListWindowNotice url="{endpoint}"' in source, (
            f"{page} 列出了 {endpoint} 却没有接窗口提示——操作员会把截断看成数据缺失"
        )
