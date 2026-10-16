"""create_pptx slides. The layout is a closed set; which optional fields a layout uses is checked here."""

from typing import Literal

from app.core import limits
from app.core.exceptions import validation_error
from app.core.safety import validate_attachment_id, validate_text
from app.schemas.common import PlainText, StrictModel, TableCell, cell_text

Layout = Literal["title", "title_and_content", "section", "two_content", "table", "image"]


class SlideTable(StrictModel):
    headers: list[PlainText]
    rows: list[list[TableCell]] = []


class SlideSpec(StrictModel):
    layout: Layout
    title: PlainText
    subtitle: PlainText | None = None
    bullets: list[PlainText] | None = None
    right_bullets: list[PlainText] | None = None
    table: SlideTable | None = None
    image_attachment_id: str | None = None
    notes: PlainText | None = None


_ALLOWED = {
    "title": {"subtitle"},
    "section": {"subtitle"},
    "title_and_content": {"bullets"},
    "two_content": {"bullets", "right_bullets"},
    "table": {"table"},
    "image": {"image_attachment_id"},
}
_OPTIONAL = ("subtitle", "bullets", "right_bullets", "table", "image_attachment_id")


def image_ids(slides: list[SlideSpec]) -> list[str]:
    seen: dict[str, None] = {}
    for s in slides:
        if s.image_attachment_id:
            seen.setdefault(s.image_attachment_id, None)
    return list(seen)


def _bullets(field: str, items: list[str] | None) -> None:
    if items is None:
        return
    if len(items) > limits.MAX_SLIDE_BULLETS:
        raise validation_error(f"{field} exceeds {limits.MAX_SLIDE_BULLETS}")
    for i, item in enumerate(items):
        validate_text(f"{field}[{i}]", item, limits.MAX_SHORT_TEXT_CHARS, multiline=True)


def _table(p: str, t: SlideTable) -> None:
    if not t.headers:
        raise validation_error(f"{p}.headers must contain at least one header")
    if len(t.headers) > limits.MAX_SLIDE_TABLE_COLUMNS:
        raise validation_error(f"{p}.headers exceeds {limits.MAX_SLIDE_TABLE_COLUMNS} columns")
    if len(t.rows) > limits.MAX_SLIDE_TABLE_ROWS:
        raise validation_error(f"{p}.rows exceeds {limits.MAX_SLIDE_TABLE_ROWS}")
    for i, h in enumerate(t.headers):
        validate_text(f"{p}.headers[{i}]", h, limits.MAX_SHORT_TEXT_CHARS, multiline=False)
    for r, row in enumerate(t.rows):
        if len(row) > len(t.headers):
            raise validation_error(f"{p}.rows[{r}] has {len(row)} cells but the table has {len(t.headers)} headers")
        for c, value in enumerate(row):
            validate_text(f"{p}.rows[{r}][{c}]", cell_text(value), limits.MAX_SHORT_TEXT_CHARS, multiline=True)


def validate_slides(slides: list[SlideSpec]) -> None:
    if not slides:
        raise validation_error("slides must contain at least one slide")
    if len(slides) > limits.MAX_SLIDES:
        raise validation_error(f"slides exceeds {limits.MAX_SLIDES}")
    for i, s in enumerate(slides):
        p = f"slides[{i}]"
        for name in _OPTIONAL:
            if getattr(s, name) is not None and name not in _ALLOWED[s.layout]:
                raise validation_error(f'{p}: field "{name}" is not used by the {s.layout} layout')
        if not s.title.strip():
            raise validation_error(f"{p}.title is required")
        validate_text(f"{p}.title", s.title, limits.MAX_SHORT_TEXT_CHARS, multiline=False)
        if s.subtitle is not None:
            validate_text(f"{p}.subtitle", s.subtitle, limits.MAX_SHORT_TEXT_CHARS, multiline=True)
        _bullets(f"{p}.bullets", s.bullets)
        _bullets(f"{p}.right_bullets", s.right_bullets)
        if s.notes is not None:
            validate_text(f"{p}.notes", s.notes, limits.MAX_TEXT_CHARS, multiline=True)
        if s.layout == "table":
            if s.table is None:
                raise validation_error(f"{p}.table is required for the table layout")
            _table(f"{p}.table", s.table)
        if s.layout == "image":
            if s.image_attachment_id is None:
                raise validation_error(f"{p}.image_attachment_id is required for the image layout")
            try:
                validate_attachment_id(s.image_attachment_id)
            except Exception:
                raise validation_error(f"{p}.image_attachment_id is not a valid attachment id") from None
    if len(image_ids(slides)) > limits.MAX_IMAGES_PER_FILE:
        raise validation_error(f"slides use more than {limits.MAX_IMAGES_PER_FILE} images")
