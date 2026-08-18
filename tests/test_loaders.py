"""Regression coverage for the xlsx loader's read-only path.

Some real bank exports declare a bogus <dimension> tag (seen as low as "A1"
on an actual 19k-row, 3MB statement) that openpyxl's fast read-only mode
trusts by default, silently truncating the sheet to almost nothing. These
tests build a small xlsx in memory, deliberately corrupt its dimension tag
the same way, and confirm the loader still reads every row correctly -
including merged-cell spreading and a row with zero XML representation.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

openpyxl = pytest.importorskip("openpyxl")

from bsconv.loaders import _load_xlsx  # noqa: E402


def _build_xlsx_with_broken_dimension() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    # Row 1: a horizontal merge (header label spanning columns).
    ws["A1"] = "Col A"
    ws["B1"] = "Merged Header"
    ws.merge_cells("B1:D1")

    # Row 2: ordinary data.
    ws.append(["x1", "x2", "x3", "x4", "x5"])

    # Row 3 deliberately untouched: a genuine gap, no <row r="3"> element at
    # all once saved - not merely an empty row.

    # Row 4: a vertical merge (A4:A6), which must NOT be spread downward.
    ws["A4"] = "r4a"
    ws["B4"] = 100
    ws.merge_cells("A4:A6")

    # Trailing data past the vertical merge.
    ws["C7"] = "last"

    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    # Corrupt the declared dimension the way the real broken export did:
    # collapse it to a single cell far smaller than the actual content.
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        names = src.namelist()
        contents = {n: src.read(n) for n in names}
    sheet_xml = contents["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert re.search(r'<dimension ref="A1:[A-Z]+\d+"', sheet_xml), (
        "test setup assumption broken: openpyxl didn't write the expected dimension tag"
    )
    contents["xl/worksheets/sheet1.xml"] = re.sub(
        r'<dimension ref="[^"]+"', '<dimension ref="A1"', sheet_xml
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, content in contents.items():
            dst.writestr(name, content)
    return out.getvalue()


def test_broken_dimension_tag_does_not_truncate_the_sheet():
    data = _build_xlsx_with_broken_dimension()
    grids = _load_xlsx(data)
    assert len(grids) == 1
    rows = grids[0].rows
    # 7 rows total (1-indexed rows 1..7), despite the sheet claiming just "A1".
    assert len(rows) == 7


def test_horizontal_merge_is_spread_across_columns():
    data = _build_xlsx_with_broken_dimension()
    rows = _load_xlsx(data)[0].rows
    # B1:D1 merged -> the header value must appear in B, C, and D of row 1.
    assert rows[0][1] == "Merged Header"
    assert rows[0][2] == "Merged Header"
    assert rows[0][3] == "Merged Header"


def test_vertical_merge_is_not_spread_downward():
    data = _build_xlsx_with_broken_dimension()
    rows = _load_xlsx(data)[0].rows
    # A4:A6 merged vertically - value stays on row 4 only, rows 5-6 stay empty.
    assert rows[3][0] == "r4a"
    assert rows[4][0] is None
    assert rows[5][0] is None


def test_gap_row_with_no_xml_element_keeps_row_alignment():
    data = _build_xlsx_with_broken_dimension()
    rows = _load_xlsx(data)[0].rows
    # Row 3 (index 2) was never touched when building the fixture, so it has
    # no <row> element in the saved XML at all - it must still show up as an
    # empty row at the correct position, not be skipped and shift row 4 up.
    assert all(v is None for v in rows[2])
    assert rows[3][0] == "r4a"  # row 4 data still lands on row 4, not row 3
    assert rows[6][2] == "last"  # row 7 data still lands on row 7
