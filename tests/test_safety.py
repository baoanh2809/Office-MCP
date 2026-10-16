"""Input checks before any Office library opens user bytes (section 3.4)."""

import io
import zipfile

import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image

from app.core import limits, safety
from app.core.exceptions import ErrorCode, OfficeDocsError


def _code(fn, *args, **kwargs) -> ErrorCode:
    with pytest.raises(OfficeDocsError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def _docx_bytes() -> bytes:
    buf = io.BytesIO()
    Document().save(buf)
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    Workbook().save(buf)
    return buf.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _with_entry(data: bytes, name: str, payload: bytes) -> bytes:
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


# --- extension / content type -------------------------------------------------


@pytest.mark.parametrize("name,kind", [("a.docx", "docx"), ("B.XLSX", "xlsx"), ("deck.pptx", "pptx")])
def test_kind_from_filename(name, kind):
    assert safety.kind_from_filename(name) == kind


@pytest.mark.parametrize("name", ["a.doc", "a.xls", "a.ppt", "a.docm", "a.xlsm", "a.pptm", "a.pdf", "noext"])
def test_legacy_macro_and_other_extensions_are_unsupported(name):
    assert _code(safety.kind_from_filename, name) == ErrorCode.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("ct", [safety.MIME["docx"], "application/octet-stream", "", None, "application/zip"])
def test_matching_or_generic_content_type_accepted(ct):
    safety.check_content_type("docx", ct)


@pytest.mark.parametrize(
    "ct", [safety.MIME["xlsx"], "application/vnd.ms-word.document.macroEnabled.12", "application/msword", "text/plain"]
)
def test_mismatched_content_type_rejected(ct):
    assert _code(safety.check_content_type, "docx", ct) == ErrorCode.UNSUPPORTED_FORMAT


# --- package inspection --------------------------------------------------------


def test_valid_packages_pass():
    safety.inspect_package(_docx_bytes(), "docx")
    safety.inspect_package(_xlsx_bytes(), "xlsx")


def test_non_zip_is_corrupt():
    assert _code(safety.inspect_package, b"hello world", "docx") == ErrorCode.CORRUPT_FILE


def test_ole_compound_file_is_unsupported():
    ole = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 512
    assert _code(safety.inspect_package, ole, "docx") == ErrorCode.UNSUPPORTED_FORMAT


def test_truncated_zip_is_corrupt():
    data = _docx_bytes()
    assert _code(safety.inspect_package, data[: len(data) // 2], "docx") == ErrorCode.CORRUPT_FILE


def test_fake_vba_project_is_rejected():
    data = _with_entry(_docx_bytes(), "word/vbaProject.bin", b"fake vba")
    assert _code(safety.inspect_package, data, "docx") == ErrorCode.UNSUPPORTED_FORMAT


def test_macro_enabled_content_type_is_rejected():
    ct = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'
    )
    data = _zip({"[Content_Types].xml": ct.encode(), "word/document.xml": b"<w:document/>"})
    assert _code(safety.inspect_package, data, "docx") == ErrorCode.UNSUPPORTED_FORMAT


def test_docx_bytes_named_xlsx_is_unsupported():
    assert _code(safety.inspect_package, _docx_bytes(), "xlsx") == ErrorCode.UNSUPPORTED_FORMAT


def test_doctype_in_any_part_is_rejected():
    data = _with_entry(_docx_bytes(), "word/evil.xml", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "a">]><x/>')
    assert _code(safety.inspect_package, data, "docx") == ErrorCode.CORRUPT_FILE


def test_small_zip_bomb_is_blocked():
    # 4 MiB of zeros deflates to a few KiB: compression ratio far above 100.
    data = _with_entry(_docx_bytes(), "word/media/big.bin", b"\x00" * (4 * 1024 * 1024))
    assert len(data) < 64 * 1024
    assert _code(safety.inspect_package, data, "docx") == ErrorCode.CORRUPT_FILE


def test_total_uncompressed_size_limit(monkeypatch):
    monkeypatch.setattr(limits, "MAX_UNCOMPRESSED_BYTES", 1000)
    assert _code(safety.inspect_package, _docx_bytes(), "docx") == ErrorCode.CORRUPT_FILE


def test_entry_count_limit(monkeypatch):
    monkeypatch.setattr(limits, "MAX_ZIP_ENTRIES", 5)
    assert _code(safety.inspect_package, _docx_bytes(), "docx") == ErrorCode.CORRUPT_FILE


def test_input_size_limit(monkeypatch):
    monkeypatch.setattr(limits, "MAX_INPUT_FILE_BYTES", 100)
    assert _code(safety.inspect_package, _docx_bytes(), "docx") == ErrorCode.FILE_TOO_LARGE


# --- images -------------------------------------------------------------------


def _image(fmt: str, size=(4, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt,expected", [("PNG", "png"), ("JPEG", "jpeg")])
def test_png_and_jpeg_images_accepted(fmt, expected):
    info = safety.check_image(_image(fmt))
    assert (info.format, info.width, info.height) == (expected, 4, 3)


def test_other_image_formats_rejected():
    assert _code(safety.check_image, _image("GIF")) == ErrorCode.UNSUPPORTED_FORMAT
    assert _code(safety.check_image, b"<svg xmlns='http://www.w3.org/2000/svg'/>") == ErrorCode.UNSUPPORTED_FORMAT


def test_image_byte_limit(monkeypatch):
    monkeypatch.setattr(limits, "MAX_IMAGE_BYTES", 10)
    assert _code(safety.check_image, _image("PNG")) == ErrorCode.FILE_TOO_LARGE


def test_image_pixel_limit(monkeypatch):
    monkeypatch.setattr(limits, "MAX_IMAGE_PIXELS", 10)
    assert _code(safety.check_image, _image("PNG")) == ErrorCode.FILE_TOO_LARGE


def test_broken_png_is_corrupt():
    assert _code(safety.check_image, b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) == ErrorCode.CORRUPT_FILE


# --- formula injection --------------------------------------------------------


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd", "=cmd|' /C calc'!A0"])
def test_formula_like_text_detected(text):
    assert safety.is_formula_like(text)


@pytest.mark.parametrize("text", ["hello", "1+1", "", " =1"])
def test_plain_text_not_formula_like(text):
    assert not safety.is_formula_like(text)


def test_valid_formula_accepted():
    assert safety.validate_formula("=SUM(B2:B10)") == "=SUM(B2:B10)"


@pytest.mark.parametrize(
    "formula",
    ["SUM(A1)", "", "=", " = ", '=WEBSERVICE("http://x")', "=cmd|' /C calc'!A0", '=call("kernel32")', "=" + "1" * 9000],
)
def test_unsafe_or_malformed_formula_rejected(formula):
    assert _code(safety.validate_formula, formula) == ErrorCode.VALIDATION_ERROR


# --- filenames, text, ids -----------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [("Q3 report", "Q3 report.docx"), ("Q3 report.DOCX", "Q3 report.DOCX"), ("v1.2 notes", "v1.2 notes.docx")],
)
def test_output_filename_gets_extension(name, expected):
    assert safety.output_filename(name, "docx") == expected


@pytest.mark.parametrize(
    "name", ["", "   ", "...", "../etc/passwd", "a/b.docx", "a\\b", "x" * 300, "bad\x00name", "a.xlsx"]
)
def test_bad_output_filenames_rejected(name):
    assert _code(safety.output_filename, name, "docx") == ErrorCode.VALIDATION_ERROR


def test_edited_filename_derives_from_source():
    assert safety.edited_filename("Budget.xlsx", "xlsx") == "Budget (edited).xlsx"
    assert safety.edited_filename("../weird/..", "docx") == "document (edited).docx"


def test_validate_text():
    safety.validate_text("blocks[0].text", "Tab\tand\nnewline ok", 100, multiline=True)
    for value, multiline, max_chars in [("a\x01b", True, 100), ("line\nbreak", False, 100), ("ééé", True, 2)]:
        assert _code(safety.validate_text, "f", value, max_chars, multiline=multiline) == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("value", ["att_6f1c2a", "att:1", "A-b.c_9"])
def test_attachment_id_accepted(value):
    safety.validate_attachment_id(value)


@pytest.mark.parametrize("value", ["", "../x", "a/b", "a b", "x" * 200, "..", "-lead"])
def test_attachment_id_rejected(value):
    assert _code(safety.validate_attachment_id, value) == ErrorCode.VALIDATION_ERROR
