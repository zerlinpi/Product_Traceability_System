"""Exports stay correct while staying bounded.

Why this file exists
--------------------
Two exports were building their payload in a way that cost far more than the
result: the workbook assembled the whole sheet three times over (a row-per-string
list, the joined copy, then an f-string copy), and the code-set export read each
set's parts with its own query. Measured on 2,000 purchase orders:

    /api/purchase-orders/export   48.4 MB peak -> 3.5 MB, 1496 ms -> 326 ms

Neither was wrong, which is why nothing caught them. The tests below assert the
properties that keep them from coming back: the bytes are still a valid workbook
with the same rows, the formula guard still applies, and the peak allocation stays
in proportion to the output rather than to its intermediate forms.
"""

from __future__ import annotations

import io
import sqlite3
import sys
import tracemalloc
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capability_helpers import make_auth_app  # noqa: E402
from traceability.code_sets import code_set_dicts, parts_by_code_set  # noqa: E402
from traceability.xlsx_export import build_traceability_xlsx  # noqa: E402

HEADERS = ["序号", "名称", "识别码", "数量"]


def sheet_xml(workbook: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


def rows(count: int) -> list[list[object]]:
    return [[index, f"产品 {index}", f"TW-{index:06d}", index * 1.5] for index in range(1, count + 1)]


# --------------------------------------------------------------------------
# The workbook is still a workbook
# --------------------------------------------------------------------------


def test_the_streamed_sheet_keeps_its_structure():
    """Freezing, filtering and the column widths live in the same part."""
    xml = sheet_xml(build_traceability_xlsx(HEADERS, rows(10)))

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    assert "<sheetData>" in xml and "</sheetData>" in xml
    assert xml.rstrip().endswith("</worksheet>")
    assert "state=\"frozen\"" in xml, "冻结首行丢失"
    assert "<autoFilter" in xml, "自动筛选丢失"
    assert "<cols>" in xml, "列宽丢失"


def test_every_row_and_cell_survives_the_streaming_write():
    data = rows(250)
    xml = sheet_xml(build_traceability_xlsx(HEADERS, data))

    assert xml.count("<row ") == len(data) + 1, "行数不符（含表头）"
    for row_index in (2, 100, 251):
        assert f'<row r="{row_index}">' in xml or f'<row r="{row_index}" ' in xml
    assert "TW-000001" in xml and "TW-000250" in xml


def test_the_header_row_keeps_its_height():
    """The taller header is a presentation detail the export promised."""
    xml = sheet_xml(build_traceability_xlsx(HEADERS, rows(3)))

    assert 'ht="24"' in xml and 'customHeight="1"' in xml


def test_the_autofilter_covers_the_last_column_and_row():
    xml = sheet_xml(build_traceability_xlsx(HEADERS, rows(7)))

    assert 'ref="A1:D8"' in xml, "自动筛选范围不对"


def test_numeric_cells_are_still_numbers():
    xml = sheet_xml(build_traceability_xlsx(HEADERS, rows(2)))

    assert 't="n"' in xml, "数字被当成了文本"


def test_the_formula_guard_still_applies():
    """Streaming must not have dropped the quotePrefix style on risky text."""
    xml = sheet_xml(build_traceability_xlsx(HEADERS, [[1, "=cmd|'/c calc'!A1", "TW-1", 2]]))

    # Body style 4, zebra body style 5 — both carry quotePrefix.
    assert 's="4"' in xml or 's="5"' in xml, "公式样文本没有套用 quotePrefix 样式"


def test_an_empty_export_still_produces_a_readable_workbook():
    workbook = build_traceability_xlsx(HEADERS, [])

    xml = sheet_xml(workbook)
    assert xml.count("<row ") == 1, "空导出应只有表头"
    assert 'ref="A1:D1"' in xml


# --------------------------------------------------------------------------
# Bounded, not just correct
# --------------------------------------------------------------------------


def test_building_a_large_sheet_stays_in_proportion_to_the_output():
    """The regression this guards: peak was 145x the workbook.

    A generous factor is used on purpose. The point is to catch a return to
    holding the sheet two or three times over, not to pin an exact figure that
    would fail on a different Python build.
    """
    data = rows(2_000)

    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        workbook = build_traceability_xlsx(HEADERS, data)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    sheet_bytes = len(sheet_xml(workbook).encode("utf-8"))
    assert peak < sheet_bytes * 3, (
        f"峰值 {peak / 1024 / 1024:.1f} MB 相对工作表 {sheet_bytes / 1024 / 1024:.1f} MB 过高"
    )


# --------------------------------------------------------------------------
# The code-set export reads parts in batches
# --------------------------------------------------------------------------


@pytest.fixture()
def sets(tmp_path):
    """A database with 120 code sets, each with two parts."""
    app, database_path, _fake = make_auth_app(tmp_path)
    connection = sqlite3.connect(str(database_path))
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            "INSERT INTO suppliers(supplier_code, name, created_at, updated_at) "
            "VALUES ('SUP-CS', '套码测试供应商', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO part_types(part_code, name, supplier_id, created_at, updated_at) "
            "VALUES ('PT-CS', '套码测试部件', 1, 'now', 'now')"
        )
        # product_code_sets.trace_plan_id is a required foreign key.
        connection.execute(
            "INSERT INTO trace_plans(model_code, name, version, created_at) "
            "VALUES ('TW-BASE', '套码测试计划', 'v1', 'now')"
        )
        for index in range(120):
            cursor = connection.execute(
                "INSERT INTO machines(sn, model, identification_code, created_at) "
                "VALUES (?, 'M', ?, 'now')",
                (f"SN-{index:04d}", f"MC-{index:04d}"),
            )
            machine_id = cursor.lastrowid
            cursor = connection.execute(
                "INSERT INTO product_code_sets(product_model_id, trace_plan_id, machine_id, "
                "prefix, date_code, daily_sequence, set_code, generated_at) "
                "VALUES (1, 1, ?, 'TW', '260101', ?, ?, 'now')",
                (machine_id, index + 1, f"SET-{index:04d}"),
            )
            set_id = cursor.lastrowid
            for position in (1, 2):
                cursor = connection.execute(
                    "INSERT INTO part_labels(part_type_id, identification_code, created_at) "
                    "VALUES (1, ?, 'now')",
                    (f"PL-{index:04d}-{position}",),
                )
                connection.execute(
                    "INSERT INTO product_code_set_parts(product_code_set_id, part_label_id, "
                    "position, slot_name) VALUES (?, ?, ?, ?)",
                    (set_id, cursor.lastrowid, position, f"槽位{position}"),
                )
        connection.commit()
    finally:
        connection.close()
    return app, database_path


def test_the_batched_reader_matches_the_per_row_reader(sets):
    """Same answer, or the optimisation changed behaviour."""
    from traceability.code_sets import product_code_set_dict

    app, _database = sets
    with app.app_context():
        from traceability.db import get_db

        database = get_db()
        found = database.execute(
            "SELECT pcs.*, pm.name AS product_name, m.sn AS machine_sn, "
            "m.identification_code AS machine_code, '历史批次' AS batch_code "
            "FROM product_code_sets pcs "
            "JOIN product_models pm ON pm.id = pcs.product_model_id "
            "JOIN machines m ON m.id = pcs.machine_id "
            "ORDER BY pcs.id LIMIT 40"
        ).fetchall()

        one_at_a_time = [product_code_set_dict(database, row) for row in found]
        batched = code_set_dicts(database, found)

    assert batched == one_at_a_time


def test_reading_parts_in_batches_issues_far_fewer_queries(sets):
    """The regression: a query per set, which a 10,000-code export multiplied."""
    app, _database = sets
    with app.app_context():
        from traceability.db import get_db

        database = get_db()
        found = database.execute(
            "SELECT pcs.*, pm.name AS product_name, m.sn AS machine_sn, "
            "m.identification_code AS machine_code, '历史批次' AS batch_code "
            "FROM product_code_sets pcs "
            "JOIN product_models pm ON pm.id = pcs.product_model_id "
            "JOIN machines m ON m.id = pcs.machine_id "
            "ORDER BY pcs.id"
        ).fetchall()
        assert len(found) == 120

        statements: list[str] = []
        database.set_trace_callback(statements.append)
        try:
            code_set_dicts(database, found)
        finally:
            database.set_trace_callback(None)

    part_queries = [sql for sql in statements if "product_code_set_parts" in sql]
    assert len(part_queries) == 1, f"部件查询应为 1 次，实际 {len(part_queries)} 次"


def test_parts_are_grouped_by_set_and_keep_their_slot_order(sets):
    app, _database = sets
    with app.app_context():
        from traceability.db import get_db

        database = get_db()
        ids = [row[0] for row in database.execute("SELECT id FROM product_code_sets ORDER BY id")]
        grouped = parts_by_code_set(database, ids)

    assert set(grouped) == set(ids)
    for set_id, parts in grouped.items():
        assert len(parts) == 2, set_id
        assert [part["position"] for part in parts] == [1, 2], "槽位顺序被打乱"


def test_an_empty_request_does_not_run_a_query(sets):
    app, _database = sets
    with app.app_context():
        from traceability.db import get_db

        database = get_db()
        statements: list[str] = []
        database.set_trace_callback(statements.append)
        try:
            grouped = parts_by_code_set(database, [])
        finally:
            database.set_trace_callback(None)

    assert grouped == {}
    assert not statements, "空请求不应查库"
