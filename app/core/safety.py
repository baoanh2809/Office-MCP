"""Checks that run before any Office library opens user bytes, plus formula-injection and text rules."""

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass

from PIL import Image

from app.core import limits
from app.core.exceptions import (
    MSG_CORRUPT_FILE,
    MSG_FILE_TOO_LARGE,
    MSG_INVALID_ATTACHMENT_ID,
    MSG_LEGACY,
    MSG_MACRO,
    ErrorCode,
    OfficeDocsError,
    corrupt_file,
    validation_error,
)

MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Main-part content types inside [Content_Types].xml.
_MAIN_PART = {
    "docx": "wordprocessingml.document.main+xml",
    "xlsx": "spreadsheetml.sheet.main+xml",
    "pptx": "presentationml.presentation.main+xml",
}

_LEGACY_EXT = {".doc", ".xls", ".ppt", ".dot", ".xlt", ".pot", ".pps"}
_MACRO_EXT = {".docm", ".dotm", ".xlsm", ".xltm", ".xlsb", ".xlam", ".pptm", ".potm", ".ppsm", ".ppam"}
_KNOWN_DOC_EXT = _LEGACY_EXT | _MACRO_EXT | {".docx", ".xlsx", ".pptx", ".pdf", ".csv", ".txt", ".md"}
_LEGACY_MIME = {"application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"}
_GENERIC_MIME = {"", "application/octet-stream", "application/zip", "application/x-zip-compressed"}

_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_ZIP_SIGNATURE = b"PK\x03\x04"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_XML_PROLOG_SCAN_BYTES = 64 * 1024
_ROOT_ELEMENT = re.compile(rb"<[A-Za-z_]")

_FILENAME_FORBIDDEN = set('/\\:*?"<>|')
_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _unsupported(message: str) -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.UNSUPPORTED_FORMAT, message)


# --- format detection ---------------------------------------------------------


def kind_from_filename(filename: str) -> str:
    ext = posixpath.splitext((filename or "").replace("\\", "/").lower())[1]
    if ext in (".docx", ".xlsx", ".pptx"):
        return ext[1:]
    if ext in _LEGACY_EXT:
        raise _unsupported(MSG_LEGACY)
    if ext in _MACRO_EXT:
        raise _unsupported(MSG_MACRO)
    raise _unsupported("Only .docx, .xlsx, and .pptx files are supported")


def check_content_type(kind: str, content_type: str | None) -> None:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct == MIME[kind] or ct in _GENERIC_MIME:
        return
    if "macroenabled" in ct:
        raise _unsupported(MSG_MACRO)
    if ct in _LEGACY_MIME:
        raise _unsupported(MSG_LEGACY)
    raise _unsupported(f"Attachment content type does not match the .{kind} extension")


def inspect_package(data: bytes, kind: str) -> None:
    """Validate an OOXML zip before python-docx / openpyxl / python-pptx touch it.

    Checks size, signature, entry count, expanded size, compression ratio, macros, main part type, and
    DTDs. zipfile also refuses to read past an entry's declared uncompressed size.
    """
    if len(data) > limits.MAX_INPUT_FILE_BYTES:
        raise OfficeDocsError(ErrorCode.FILE_TOO_LARGE, MSG_FILE_TOO_LARGE)
    if data.startswith(_OLE_SIGNATURE):
        raise _unsupported("Legacy or password-protected Office files are not supported")
    if not data.startswith(_ZIP_SIGNATURE):
        raise corrupt_file()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError):
        raise corrupt_file() from None

    with zf:
        if len(infos) > limits.MAX_ZIP_ENTRIES:
            raise corrupt_file("File contains too many parts")
        total = sum(i.file_size for i in infos)
        packed = sum(i.compress_size for i in infos)
        if total > limits.MAX_UNCOMPRESSED_BYTES:
            raise corrupt_file("File expands beyond the size limit")
        if total / max(packed, 1) > limits.MAX_COMPRESSION_RATIO:
            raise corrupt_file("File compression ratio is too high")
        names = {i.filename for i in infos}
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise _unsupported(MSG_MACRO)
        if "[Content_Types].xml" not in names:
            raise corrupt_file()
        content_types = _read(zf, "[Content_Types].xml", 1 * limits.MIB).decode("utf-8", errors="replace").lower()
        if "macroenabled" in content_types:
            raise _unsupported(MSG_MACRO)
        if _MAIN_PART[kind] not in content_types:
            raise _unsupported(f"File content is not a .{kind} file")
        for info in infos:
            if info.filename.lower().endswith((".xml", ".rels", ".vml")):
                _check_xml_prolog(zf, info)


def _read(zf: zipfile.ZipFile, name: str | zipfile.ZipInfo, max_bytes: int) -> bytes:
    try:
        with zf.open(name) as fh:
            return fh.read(max_bytes)
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, KeyError):
        raise corrupt_file() from None


def _check_xml_prolog(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    """OOXML never needs a DTD. Reject DOCTYPE/ENTITY before the root element (XXE, entity expansion)."""
    head = _read(zf, info, _XML_PROLOG_SCAN_BYTES)
    root = _ROOT_ELEMENT.search(head)
    if root is None and len(head) >= _XML_PROLOG_SCAN_BYTES:
        raise corrupt_file()
    prolog = (head if root is None else head[: root.start()]).upper()
    if b"<!DOCTYPE" in prolog or b"<!ENTITY" in prolog:
        raise corrupt_file("File contains a document type declaration")


# --- images -------------------------------------------------------------------


@dataclass(frozen=True)
class ImageInfo:
    format: str  # png | jpeg
    width: int
    height: int


def check_image(data: bytes) -> ImageInfo:
    """Accept PNG and JPEG within the byte and pixel limits."""
    if len(data) > limits.MAX_IMAGE_BYTES:
        raise OfficeDocsError(ErrorCode.FILE_TOO_LARGE, "Image exceeds 10 MiB")
    if data.startswith(_PNG_SIGNATURE):
        expected = "PNG"
    elif data.startswith(_JPEG_SIGNATURE):
        expected = "JPEG"
    else:
        raise _unsupported("Images must be PNG or JPEG")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt, (width, height) = im.format, im.size
            if width * height > limits.MAX_IMAGE_PIXELS:
                raise OfficeDocsError(ErrorCode.FILE_TOO_LARGE, "Image dimensions exceed the limit")
            im.verify()
    except OfficeDocsError:
        raise
    except Image.DecompressionBombError:
        raise OfficeDocsError(ErrorCode.FILE_TOO_LARGE, "Image dimensions exceed the limit") from None
    except Exception:
        raise corrupt_file("Image could not be opened") from None
    if fmt != expected or width <= 0 or height <= 0:
        raise _unsupported("Images must be PNG or JPEG")
    return ImageInfo(format=expected.lower(), width=width, height=height)


# --- formula injection --------------------------------------------------------

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# External-call functions and DDE. Formulas are written only from the explicit ``formula`` field, but the
# agent may still copy text from an untrusted file into it.
_BLOCKED_FORMULA = re.compile(
    r"\b(WEBSERVICE|CALL|REGISTER(\.ID)?|EXEC|RTD|DDE|DDEAUTO|IMPORT(XML|DATA|HTML|FEED|RANGE))\s*\("
)


def is_formula_like(text: str) -> bool:
    """True when a spreadsheet app could treat this text as a formula."""
    return text.startswith(_FORMULA_PREFIXES)


def validate_formula(formula: str) -> str:
    if not formula.startswith("=") or len(formula.strip()) < 2:
        raise validation_error("formula must start with '=' and contain an expression")
    if len(formula) > limits.MAX_FORMULA_CHARS:
        raise validation_error("formula exceeds 8192 characters")
    if "|" in formula or _BLOCKED_FORMULA.search(formula.upper()):
        raise validation_error("formula uses an external-call function or DDE, which is not allowed")
    return formula


# --- filenames, text, ids -----------------------------------------------------


def output_filename(name: str, kind: str) -> str:
    name = (name or "").strip()
    if not name.strip(". "):
        raise validation_error("filename is empty")
    if any(ch in _FILENAME_FORBIDDEN or ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise validation_error('filename must not contain path separators or any of : * ? " < > |')
    if not name.lower().endswith("." + kind):
        if posixpath.splitext(name.lower())[1] in _KNOWN_DOC_EXT:
            raise validation_error(f"filename must end with .{kind}")
        name = f"{name}.{kind}"
    if len(name) > limits.MAX_FILENAME_CHARS:
        raise validation_error(f"filename exceeds {limits.MAX_FILENAME_CHARS} characters")
    return name


def edited_filename(source_filename: str, kind: str) -> str:
    """Derive "<stem> (edited).kind" from the source attachment's filename."""
    base = (source_filename or "").replace("\\", "/").split("/")[-1]
    stem = base[: -(len(kind) + 1)] if base.lower().endswith("." + kind) else posixpath.splitext(base)[0]
    stem = stem.strip()[: limits.MAX_FILENAME_CHARS - 20]
    fallback = f"document (edited).{kind}"
    if not stem.strip(". "):
        return fallback
    try:
        return output_filename(f"{stem} (edited)", kind)
    except OfficeDocsError:
        return fallback


def validate_text(field: str, text: str, max_chars: int, *, multiline: bool) -> None:
    """Reject control characters Office files cannot store and over-long strings.

    ``multiline`` allows newline and tab.
    """
    if len(text) > max_chars:
        raise validation_error(f"{field} exceeds {max_chars} characters")
    for ch in text:
        code = ord(ch)
        if ch in "\n\t\r":
            if not multiline:
                raise validation_error(f"{field} must be a single line without tabs")
            continue
        if code < 32 or code in (0xFFFE, 0xFFFF) or 0xD800 <= code <= 0xDFFF:
            raise validation_error(f"{field} contains control characters that Office files cannot store")


def validate_attachment_id(value: str) -> None:
    """Reject ids that cannot be a real attachment id (empty, path tricks, odd characters)."""
    if not value or len(value) > limits.MAX_ATTACHMENT_ID_CHARS or ".." in value or not _ATTACHMENT_ID.match(value):
        raise validation_error(MSG_INVALID_ATTACHMENT_ID)


__all__ = [
    "MIME",
    "MSG_CORRUPT_FILE",
    "ImageInfo",
    "check_content_type",
    "check_image",
    "edited_filename",
    "inspect_package",
    "is_formula_like",
    "kind_from_filename",
    "output_filename",
    "validate_attachment_id",
    "validate_formula",
    "validate_text",
]
