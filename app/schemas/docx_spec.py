"""create_docx blocks and edit_docx operations.

Shape and closed sets (block type, heading level) are checked by the MCP layer from the JSON Schema.
Limits and text rules are checked by ``validate_*`` and return VALIDATION_ERROR.
"""

from typing import Annotated, Literal

from pydantic import Field

from app.core import limits
from app.core.exceptions import validation_error
from app.core.safety import validate_attachment_id, validate_text
from app.schemas.common import PlainText, StrictModel, TableCell, cell_text


class HeadingBlock(StrictModel):
    type: Literal["heading"]
    level: Literal[1, 2, 3, 4]
    text: PlainText


class ParagraphBlock(StrictModel):
    type: Literal["paragraph"]
    text: PlainText
    bold: bool = False
    italic: bool = False


class BulletListBlock(StrictModel):
    type: Literal["bullet_list"]
    items: list[PlainText]


class NumberedListBlock(StrictModel):
    type: Literal["numbered_list"]
    items: list[PlainText]


class TableBlock(StrictModel):
    type: Literal["table"]
    headers: list[PlainText]
    rows: list[list[TableCell]] = []


class PageBreakBlock(StrictModel):
    type: Literal["page_break"]


class ImageBlock(StrictModel):
    type: Literal["image"]
    attachment_id: str
    width_cm: float | None = None


DocxBlock = Annotated[
    HeadingBlock | ParagraphBlock | BulletListBlock | NumberedListBlock | TableBlock | PageBreakBlock | ImageBlock,
    Field(discriminator="type"),
]


class ReplaceTextOp(StrictModel):
    type: Literal["replace_text"]
    find: PlainText
    replace: PlainText
    match_case: bool = True


class AppendBlocksOp(StrictModel):
    type: Literal["append_blocks"]
    blocks: list[DocxBlock]


DocxOperation = Annotated[ReplaceTextOp | AppendBlocksOp, Field(discriminator="type")]


def image_ids(blocks: list) -> list[str]:
    """Distinct image attachment ids in first-use order."""
    seen: dict[str, None] = {}
    for b in blocks:
        if isinstance(b, ImageBlock):
            seen.setdefault(b.attachment_id, None)
    return list(seen)


def _validate_block(p: str, b) -> None:
    if isinstance(b, HeadingBlock):
        if not b.text.strip():
            raise validation_error(f"{p}.text is required")
        validate_text(f"{p}.text", b.text, limits.MAX_SHORT_TEXT_CHARS, multiline=False)
    elif isinstance(b, ParagraphBlock):
        validate_text(f"{p}.text", b.text, limits.MAX_TEXT_CHARS, multiline=True)
    elif isinstance(b, BulletListBlock | NumberedListBlock):
        if not b.items:
            raise validation_error(f"{p}.items must contain at least one item")
        if len(b.items) > limits.MAX_LIST_ITEMS:
            raise validation_error(f"{p}.items exceeds {limits.MAX_LIST_ITEMS}")
        for i, item in enumerate(b.items):
            validate_text(f"{p}.items[{i}]", item, limits.MAX_TEXT_CHARS, multiline=True)
    elif isinstance(b, TableBlock):
        if not b.headers:
            raise validation_error(f"{p}.headers must contain at least one header")
        if len(b.headers) > limits.MAX_TABLE_COLUMNS:
            raise validation_error(f"{p}.headers exceeds {limits.MAX_TABLE_COLUMNS} columns")
        if len(b.rows) > limits.MAX_TABLE_ROWS:
            raise validation_error(f"{p}.rows exceeds {limits.MAX_TABLE_ROWS}")
        for i, h in enumerate(b.headers):
            validate_text(f"{p}.headers[{i}]", h, limits.MAX_SHORT_TEXT_CHARS, multiline=False)
        for r, row in enumerate(b.rows):
            if len(row) > len(b.headers):
                raise validation_error(f"{p}.rows[{r}] has {len(row)} cells but the table has {len(b.headers)} headers")
            for c, value in enumerate(row):
                validate_text(f"{p}.rows[{r}][{c}]", cell_text(value), limits.MAX_TEXT_CHARS, multiline=True)
    elif isinstance(b, ImageBlock):
        try:
            validate_attachment_id(b.attachment_id)
        except Exception:
            raise validation_error(f"{p}.attachment_id is not a valid attachment id") from None
        if b.width_cm is not None and not limits.MIN_IMAGE_WIDTH_CM <= b.width_cm <= limits.MAX_IMAGE_WIDTH_CM:
            raise validation_error(
                f"{p}.width_cm must be between {limits.MIN_IMAGE_WIDTH_CM} and {limits.MAX_IMAGE_WIDTH_CM:g}"
            )


def validate_blocks(prefix: str, blocks: list) -> None:
    if not blocks:
        raise validation_error(f"{prefix} must contain at least one block")
    if len(blocks) > limits.MAX_BLOCKS:
        raise validation_error(f"{prefix} exceeds {limits.MAX_BLOCKS} blocks")
    for i, b in enumerate(blocks):
        _validate_block(f"{prefix}[{i}]", b)
    if len(image_ids(blocks)) > limits.MAX_IMAGES_PER_FILE:
        raise validation_error(f"{prefix} uses more than {limits.MAX_IMAGES_PER_FILE} images")


def validate_operations(ops: list) -> None:
    if not ops:
        raise validation_error("operations must contain at least one operation")
    if len(ops) > limits.MAX_EDIT_OPERATIONS:
        raise validation_error(f"operations exceeds {limits.MAX_EDIT_OPERATIONS}")
    appended: list = []
    for i, op in enumerate(ops):
        p = f"operations[{i}]"
        if isinstance(op, ReplaceTextOp):
            if not op.find:
                raise validation_error(f"{p}.find must not be empty")
            validate_text(f"{p}.find", op.find, limits.MAX_SHORT_TEXT_CHARS, multiline=False)
            validate_text(f"{p}.replace", op.replace, limits.MAX_TEXT_CHARS, multiline=False)
        else:
            validate_blocks(f"{p}.blocks", op.blocks)
            appended.extend(op.blocks)
    if len(appended) > limits.MAX_BLOCKS:
        raise validation_error(f"operations append more than {limits.MAX_BLOCKS} blocks")
    if len(image_ids(appended)) > limits.MAX_IMAGES_PER_FILE:
        raise validation_error(f"operations use more than {limits.MAX_IMAGES_PER_FILE} images")


def operation_image_ids(ops: list) -> list[str]:
    return image_ids([b for op in ops if isinstance(op, AppendBlocksOp) for b in op.blocks])
