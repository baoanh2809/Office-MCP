"""Every size and count limit in one place.

Read these as ``limits.NAME`` at call time (not ``from limits import NAME``) so tests can patch them.
"""

MIB = 1024 * 1024

# Files
MAX_INPUT_FILE_BYTES = 20 * MIB  # same cap as the Attachment service
MAX_OUTPUT_FILE_BYTES = 20 * MIB  # larger outputs return OUTPUT_TOO_LARGE instead of uploading
MAX_IMAGE_BYTES = 10 * MIB
MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard for png/jpeg
MAX_IMAGES_PER_FILE = 50

# Zip (OOXML package) inspection
MAX_UNCOMPRESSED_BYTES = 200 * MIB
MAX_COMPRESSION_RATIO = 100
MAX_ZIP_ENTRIES = 10_000

# Create / edit specs
MAX_BLOCKS = 2_000
MAX_LIST_ITEMS = 1_000
MAX_TABLE_COLUMNS = 50
MAX_TABLE_ROWS = 5_000
MAX_SLIDES = 200
MAX_SLIDE_BULLETS = 50
MAX_SLIDE_TABLE_ROWS = 50
MAX_SLIDE_TABLE_COLUMNS = 20
MAX_SHEETS = 100
MAX_SHEET_COLUMNS = 1_000
MAX_WORKBOOK_CELLS = 1_000_000  # cells written by one create_xlsx / update_xlsx call, headers included
MAX_CELL_TEXT_CHARS = 32_767  # Excel's own cell limit
MAX_TEXT_CHARS = 100_000  # one paragraph, list item, table cell, or note
MAX_SHORT_TEXT_CHARS = 1_000  # titles, headings, headers, bullets, find/replace
MAX_FORMULA_CHARS = 8_192  # Excel's own formula limit
MAX_EDIT_OPERATIONS = 500
MAX_CELL_UPDATES = 10_000
MAX_FILENAME_CHARS = 120
MAX_ATTACHMENT_ID_CHARS = 128
MAX_OWNER_ID_CHARS = 200
MAX_EXCEL_ROWS = 1_048_576
MAX_EXCEL_COLUMNS = 16_384
MIN_IMAGE_WIDTH_CM = 0.5
MAX_IMAGE_WIDTH_CM = 30.0

# Read responses
DEFAULT_MAX_CHARS = 100_000
MAX_MAX_CHARS = 500_000
DEFAULT_MAX_ROWS = 200
MAX_MAX_ROWS = 5_000

# HTTP
MAX_REQUEST_BODY_BYTES = 32 * MIB
STORAGE_TIMEOUT_SECONDS = 30.0


def clamp(value: int | None, default: int, maximum: int) -> int:
    """``default`` for missing or non-positive values, ``maximum`` for values above it."""
    if value is None or value <= 0:
        return default
    return min(value, maximum)
