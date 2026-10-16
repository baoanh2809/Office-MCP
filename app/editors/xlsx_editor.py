"""update_xlsx: set cells and append rows, returning a new workbook. The source is never modified.

openpyxl rewrites the whole workbook. It keeps values, formulas, styles, merged cells, data validation, and
conditional formatting, but drops charts, images, pivot tables, and Excel's cached formula values. The
result reports those losses in ``warnings``, and Excel recalculates formulas on open (fullCalcOnLoad).
"""

import io
import warnings
import zipfile

from openpyxl import load_workbook
from openpyxl.workbook.properties import CalcProperties

from app.core import limits
from app.core.exceptions import OfficeDocsError, corrupt_file, validation_error
from app.schemas.xlsx_spec import AppendRows, CellUpdate, FormulaCell

_DROPPED = (
    ("xl/charts/", "Charts in the source workbook are not carried over to the new file"),
    ("xl/drawings/", "Images and drawings in the source workbook are not carried over to the new file"),
    ("xl/pivotTables/", "Pivot tables in the source workbook are not carried over to the new file"),
)


def _warnings(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    out = [message for prefix, message in _DROPPED if any(n.startswith(prefix) for n in names)]
    if any(n.startswith("xl/worksheets/") for n in names):
        out.append("Cached formula results are cleared; Excel recalculates formulas when the file is opened")
    return out


def _set(cell, value) -> None:
    if isinstance(value, FormulaCell):
        cell.value = value.formula
        return
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"  # never let "=..." text become a formula


def update_xlsx(data: bytes, updates: list[CellUpdate], appends: list[AppendRows]) -> tuple[bytes, dict]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = load_workbook(io.BytesIO(data))
    except Exception:
        raise corrupt_file() from None
    missing = validation_error("sheet does not exist in this workbook; call read_xlsx to list sheets")
    result = {"cells_updated": 0, "rows_appended": 0, "warnings": _warnings(data)}
    try:
        for u in updates:
            if u.sheet not in wb.sheetnames:
                raise missing
            cell = wb[u.sheet][u.cell.upper()]
            _set(cell, FormulaCell(formula=u.formula) if u.formula is not None else u.value)
            result["cells_updated"] += 1
        for a in appends:
            if a.sheet not in wb.sheetnames:
                raise missing
            ws = wb[a.sheet]
            last = ws.max_row
            if last == 1 and all(c.value is None for c in ws[1]):
                last = 0
            if last + len(a.rows) > limits.MAX_EXCEL_ROWS:
                raise validation_error("append_rows would pass Excel's row limit")
            for i, row in enumerate(a.rows, start=1):
                for j, value in enumerate(row, start=1):
                    if value is not None:
                        _set(ws.cell(row=last + i, column=j), value)
                result["rows_appended"] += 1
        if wb.calculation is None:  # files without calcPr
            wb.calculation = CalcProperties()
        wb.calculation.fullCalcOnLoad = True
        out = io.BytesIO()
        wb.save(out)
    except OfficeDocsError:
        raise
    except Exception:
        raise corrupt_file() from None
    return out.getvalue(), result
