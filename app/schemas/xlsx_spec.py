"""create_xlsx sheets and update_xlsx changes.

A cell is a string, number, boolean, null, or ``{"formula": "=..."}``. Strings are always written as text, even
when they start with = + - @ tab or CR. Only the ``formula`` field creates a formula.
"""

import datetime as dt
import math
import re
from typing import Annotated, Literal

from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
from pydantic import Field

from app.core import limits
from app.core.exceptions import OfficeDocsError, validation_error
from app.core.safety import validate_formula, validate_text
from app.schemas.common import PLAIN, PlainText, StrictModel

ColumnType = Literal["string", "number", "integer", "date", "boolean", "currency", "percent"]


class FormulaCell(StrictModel):
    formula: str


Scalar = bool | int | float | str | None
CellValue = Scalar | FormulaCell
Rows = Annotated[list[list[CellValue]], Field(json_schema_extra=PLAIN)]


class Column(StrictModel):
    header: PlainText
    type: ColumnType


class SheetSpec(StrictModel):
    name: PlainText
    columns: list[Column]
    rows: Rows = []


class CellUpdate(StrictModel):
    """Exactly one of ``value`` (null clears the cell) or ``formula``."""

    sheet: PlainText
    cell: str
    value: Annotated[Scalar, Field(json_schema_extra=PLAIN)] = None
    formula: str | None = None

    @property
    def has_value(self) -> bool:
        return "value" in self.model_fields_set


class AppendRows(StrictModel):
    sheet: PlainText
    rows: Rows


_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
_CELL_REF = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$")
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_date(text: str) -> tuple[dt.datetime, bool] | None:
    """Return (value, has_time) for YYYY-MM-DD or YYYY-MM-DD[T ]HH:MM:SS, else None."""
    for i, fmt in enumerate(_DATE_FORMATS):
        try:
            return dt.datetime.strptime(text, fmt), i > 0
        except ValueError:
            continue
    return None


def validate_sheet_name(field: str, name: str, seen: set[str] | None = None) -> None:
    if not name.strip():
        raise validation_error(f"{field} is empty")
    if len(name) > 31:
        raise validation_error(f"{field} exceeds 31 characters")
    if _SHEET_FORBIDDEN.search(name) or name.startswith("'") or name.endswith("'"):
        raise validation_error(f"{field} must not contain [ ] : * ? / \\ or start or end with '")
    validate_text(field, name, 31, multiline=False)
    if name.lower() == "history":
        raise validation_error(f'{field} "History" is reserved by Excel')
    if seen is not None:
        if name.lower() in seen:
            raise validation_error(f"{field} duplicates another sheet name")
        seen.add(name.lower())


def _validate_cell(field: str, value: CellValue, col_type: str | None) -> None:
    if value is None:
        return
    if isinstance(value, FormulaCell):
        try:
            validate_formula(value.formula)
        except OfficeDocsError as err:
            raise validation_error(f"{field}: {err.message}") from None
        return
    if isinstance(value, str):
        validate_text(field, value, limits.MAX_CELL_TEXT_CHARS, multiline=True)
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(value, float) and not math.isfinite(value):
        raise validation_error(f"{field}: numbers must be finite")
    if col_type in ("number", "currency", "percent") and not is_number:
        raise validation_error(f"{field}: expected a number")
    if col_type == "integer" and not (is_number and float(value).is_integer()):
        raise validation_error(f"{field}: expected a whole number")
    if col_type == "boolean" and not isinstance(value, bool):
        raise validation_error(f"{field}: expected true or false")
    if col_type == "date" and not (isinstance(value, str) and parse_date(value)):
        raise validation_error(f'{field}: expected a date string such as "2026-01-31" or "2026-01-31T14:30:00"')


def validate_sheets(sheets: list[SheetSpec]) -> None:
    if not sheets:
        raise validation_error("sheets must contain at least one sheet")
    if len(sheets) > limits.MAX_SHEETS:
        raise validation_error(f"sheets exceeds {limits.MAX_SHEETS}")
    seen: set[str] = set()
    cells = 0
    for i, sheet in enumerate(sheets):
        p = f"sheets[{i}]"
        validate_sheet_name(f"{p}.name", sheet.name, seen)
        if not sheet.columns:
            raise validation_error(f"{p}.columns must contain at least one column")
        if len(sheet.columns) > limits.MAX_SHEET_COLUMNS:
            raise validation_error(f"{p}.columns exceeds {limits.MAX_SHEET_COLUMNS}")
        if len(sheet.rows) + 1 > limits.MAX_EXCEL_ROWS:
            raise validation_error(f"{p}.rows exceeds Excel's row limit")
        cells += (len(sheet.rows) + 1) * len(sheet.columns)
        if cells > limits.MAX_WORKBOOK_CELLS:
            raise validation_error(
                f"workbook exceeds {limits.MAX_WORKBOOK_CELLS} cells (rows x columns, headers included)"
            )
        for j, col in enumerate(sheet.columns):
            validate_text(f"{p}.columns[{j}].header", col.header, limits.MAX_SHORT_TEXT_CHARS, multiline=False)
        for r, row in enumerate(sheet.rows):
            if len(row) > len(sheet.columns):
                raise validation_error(
                    f"{p}.rows[{r}] has {len(row)} cells but the sheet has {len(sheet.columns)} columns"
                )
            for c, value in enumerate(row):
                _validate_cell(f"{p}.rows[{r}][{c}]", value, sheet.columns[c].type)


def validate_cell_ref(field: str, ref: str) -> None:
    if not _CELL_REF.match(ref):
        raise validation_error(f'{field} must be one A1 cell reference such as "B2"')
    letters, row = coordinate_from_string(ref.upper())
    if column_index_from_string(letters) > limits.MAX_EXCEL_COLUMNS or row > limits.MAX_EXCEL_ROWS:
        raise validation_error(f"{field} is outside the Excel grid")


def validate_update(updates: list[CellUpdate], appends: list[AppendRows]) -> None:
    if not updates and not appends:
        raise validation_error("provide updates, append_rows, or both")
    if len(updates) > limits.MAX_CELL_UPDATES:
        raise validation_error(f"updates exceeds {limits.MAX_CELL_UPDATES}")
    for i, u in enumerate(updates):
        p = f"updates[{i}]"
        validate_sheet_name(f"{p}.sheet", u.sheet)
        validate_cell_ref(f"{p}.cell", u.cell)
        if u.has_value and u.formula is not None:
            raise validation_error(f"{p}: send value or formula, not both")
        if not u.has_value and u.formula is None:
            raise validation_error(f"{p}: send value or formula")
        if u.formula is not None:
            _validate_cell(f"{p}.formula", FormulaCell(formula=u.formula), None)
        else:
            _validate_cell(f"{p}.value", u.value, None)
    cells = len(updates)
    for i, a in enumerate(appends):
        p = f"append_rows[{i}]"
        validate_sheet_name(f"{p}.sheet", a.sheet)
        if not a.rows:
            raise validation_error(f"{p}.rows must contain at least one row")
        for r, row in enumerate(a.rows):
            if len(row) > limits.MAX_SHEET_COLUMNS:
                raise validation_error(f"{p}.rows[{r}] exceeds {limits.MAX_SHEET_COLUMNS} cells")
            cells += len(row)
            if cells > limits.MAX_WORKBOOK_CELLS:
                raise validation_error(f"request writes more than {limits.MAX_WORKBOOK_CELLS} cells")
            for c, value in enumerate(row):
                _validate_cell(f"{p}.rows[{r}][{c}]", value, None)
