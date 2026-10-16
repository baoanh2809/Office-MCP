"""Service layer: owner checks, storage, input checks, outputs, envelopes, and logging."""

import io
import json
import logging
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from pydantic import TypeAdapter

from app.core import limits
from app.core.config import Settings
from app.core.exceptions import ErrorCode, OfficeDocsError
from app.core.owner import OwnerContext
from app.core.safety import MIME
from app.schemas.docx_spec import DocxBlock, DocxOperation
from app.schemas.pptx_spec import SlideSpec
from app.schemas.xlsx_spec import AppendRows, CellUpdate, SheetSpec
from app.services.office_service import OfficeDocsService
from app.storage.base import AttachmentStore
from app.storage.memory_store import MemoryStore

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = json.dumps({"owner_type": "work_item", "owner_id": "wi_1", "tenant_id": "default"})
OTHER = json.dumps({"owner_type": "work_item", "owner_id": "wi_2", "tenant_id": "default"})
OWNER_CTX = OwnerContext("work_item", "wi_1", "default")
BLOCKS = TypeAdapter(list[DocxBlock])
OPS = TypeAdapter(list[DocxOperation])
SHEETS = TypeAdapter(list[SheetSpec])
SLIDES = TypeAdapter(list[SlideSpec])
UPDATES = TypeAdapter(list[CellUpdate])
APPENDS = TypeAdapter(list[AppendRows])
SETTINGS = Settings(storage="memory", dev_mode=False, local_dir=None, attachments_url=None, attachments_api_key=None)


@pytest.fixture
def store():
    return MemoryStore(max_files_per_owner=5)


@pytest.fixture
def svc(store):
    return OfficeDocsService(SETTINGS, store=store)


def error_code(env: dict) -> str:
    assert env["success"] is False, env
    return env["error"]["error_code"]


def put(store, name: str, data: bytes, ct: str = "") -> str:
    return store.put(OWNER_CTX, name, ct, data).attachment_id


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 4)).save(buf, format="PNG")
    return buf.getvalue()


def test_create_docx_stores_a_new_attachment(svc, store):
    env = svc.create_docx(OWNER, "Q3 report", BLOCKS.validate_python([{"type": "paragraph", "text": "hi"}]))
    assert env["success"] is True
    assert list(env) == ["success", "attachment_id", "filename", "content_type", "size_bytes"]
    assert (env["filename"], env["content_type"]) == ("Q3 report.docx", MIME["docx"])
    data = store.get_content(env["attachment_id"], OWNER_CTX)
    assert len(data) == env["size_bytes"]
    read = svc.read_docx(OWNER, env["attachment_id"], None)
    assert read["content_trust"] == "untrusted"
    assert read["blocks"] == [{"type": "paragraph", "text": "hi"}]


def test_never_returns_file_bytes(svc):
    env = svc.create_docx(OWNER, "a", BLOCKS.validate_python([{"type": "paragraph", "text": "hi"}]))
    assert all(not isinstance(v, bytes | bytearray) for v in env.values())
    assert "base64" not in json.dumps(env)


def test_owner_context_required(svc):
    env = svc.create_docx(None, "a", BLOCKS.validate_python([{"type": "paragraph", "text": "hi"}]))
    assert error_code(env) == "OWNER_CONTEXT_REQUIRED"
    assert env["error"]["retryable"] is False


def test_validation_runs_before_owner_and_storage():
    svc = OfficeDocsService(SETTINGS, store=None, store_error=OfficeDocsError(ErrorCode.STORAGE_NOT_CONFIGURED, "x"))
    assert error_code(svc.create_docx(None, "a", BLOCKS.validate_python([]))) == "VALIDATION_ERROR"
    assert error_code(svc.create_docx(None, "a/b", BLOCKS.validate_python([{"type": "page_break"}]))) == (
        "VALIDATION_ERROR"
    )
    ok_blocks = BLOCKS.validate_python([{"type": "page_break"}])
    assert error_code(svc.create_docx(None, "a", ok_blocks)) == "OWNER_CONTEXT_REQUIRED"
    assert error_code(svc.create_docx(OWNER, "a", ok_blocks)) == "STORAGE_NOT_CONFIGURED"


def test_other_owner_is_denied_without_leaking(svc, store):
    att = put(store, "secret-plan.docx", (FIXTURES / "sample.docx").read_bytes())
    env = svc.read_docx(OTHER, att, None)
    assert error_code(env) == "PERMISSION_DENIED"
    assert "secret-plan" not in json.dumps(env)


def test_not_found_and_invalid_id(svc):
    assert error_code(svc.read_docx(OWNER, "att_missing", None)) == "NOT_FOUND"
    assert error_code(svc.read_docx(OWNER, "../etc/passwd", None)) == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "name,data,code",
    [
        ("report.xlsx", (FIXTURES / "sample.xlsx").read_bytes(), "UNSUPPORTED_FORMAT"),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "UNSUPPORTED_FORMAT"),
        ("macro.docm", b"PK\x03\x04", "UNSUPPORTED_FORMAT"),
        ("renamed.docx", (FIXTURES / "sample.xlsx").read_bytes(), "UNSUPPORTED_FORMAT"),
        ("broken.docx", b"PK\x03\x04 not really a zip", "CORRUPT_FILE"),
    ],
)
def test_input_format_checks(svc, store, name, data, code):
    assert error_code(svc.read_docx(OWNER, put(store, name, data), None)) == code


def test_input_size_checked_before_download(svc, store, monkeypatch):
    att = put(store, "big.docx", (FIXTURES / "sample.docx").read_bytes())
    monkeypatch.setattr(limits, "MAX_INPUT_FILE_BYTES", 100)
    calls = []
    original = store.get_content
    store.get_content = lambda *a: calls.append(a) or original(*a)
    assert error_code(svc.read_docx(OWNER, att, None)) == "FILE_TOO_LARGE"
    assert calls == []


def test_zip_bomb_is_corrupt(svc, store):
    buf = io.BytesIO((FIXTURES / "sample.docx").read_bytes())
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/media/bomb.bin", b"\x00" * (4 * 1024 * 1024))
    assert error_code(svc.read_docx(OWNER, put(store, "bomb.docx", buf.getvalue()), None)) == "CORRUPT_FILE"


def test_output_too_large_is_not_uploaded(svc, store, monkeypatch):
    monkeypatch.setattr(limits, "MAX_OUTPUT_FILE_BYTES", 100)
    puts = []
    original = store.put
    store.put = lambda *a: puts.append(a) or original(*a)
    env = svc.create_xlsx(
        OWNER, "big", SHEETS.validate_python([{"name": "A", "columns": [{"header": "x", "type": "string"}]}])
    )
    assert error_code(env) == "OUTPUT_TOO_LARGE"
    assert puts == []


def test_storage_limit_reached(svc):
    blocks = BLOCKS.validate_python([{"type": "page_break"}])
    for _ in range(5):
        assert svc.create_docx(OWNER, "a", blocks)["success"]
    env = svc.create_docx(OWNER, "a", blocks)
    assert error_code(env) == "STORAGE_LIMIT_REACHED" and env["error"]["retryable"] is False


def test_storage_error_is_retryable(svc):
    class Down(AttachmentStore):
        def get_metadata(self, *a):
            raise OfficeDocsError(ErrorCode.STORAGE_ERROR, "Attachment service is unavailable", "HTTP 503")

        get_content = get_metadata

        def put(self, *a):
            raise OfficeDocsError(ErrorCode.STORAGE_ERROR, "Attachment service is unavailable", "HTTP 503")

    env = OfficeDocsService(SETTINGS, store=Down()).read_docx(OWNER, "att_1", None)
    assert env["error"] == {
        "error_code": "STORAGE_ERROR",
        "error_message": "Attachment service is unavailable",
        "retryable": True,
        "original_provider_error": "HTTP 503",
    }


def test_images_come_from_attachments(svc, store):
    img = put(store, "chart.png", png(), "image/png")
    blocks = BLOCKS.validate_python([{"type": "image", "attachment_id": img}])
    assert svc.create_docx(OWNER, "with image", blocks)["success"]
    gif = io.BytesIO()
    Image.new("RGB", (2, 2)).save(gif, format="GIF")
    bad = put(store, "x.gif", gif.getvalue(), "image/gif")
    assert error_code(
        svc.create_docx(OWNER, "x", BLOCKS.validate_python([{"type": "image", "attachment_id": bad}]))
    ) == ("UNSUPPORTED_FORMAT")
    foreign = store.put(OwnerContext("work_item", "wi_2", "default"), "p.png", "image/png", png()).attachment_id
    env = svc.create_docx(OWNER, "x", BLOCKS.validate_python([{"type": "image", "attachment_id": foreign}]))
    assert error_code(env) == "PERMISSION_DENIED"


def test_edit_docx_keeps_original_and_returns_new_id(svc, store):
    original = (FIXTURES / "sample.docx").read_bytes()
    att = put(store, "Review.docx", original)
    env = svc.edit_docx(
        OWNER, att, OPS.validate_python([{"type": "replace_text", "find": "Acme", "replace": "Globex"}]), None
    )
    assert env["success"], env
    assert env["attachment_id"] != att and env["source_attachment_id"] == att
    assert env["filename"] == "Review (edited).docx"
    assert env["operations"] == [{"type": "replace_text", "replacements": 2}]
    assert store.get_content(att, OWNER_CTX) == original


def test_update_xlsx_keeps_original(svc, store):
    original = (FIXTURES / "sample.xlsx").read_bytes()
    att = put(store, "Budget.xlsx", original)
    env = svc.update_xlsx(
        OWNER,
        att,
        UPDATES.validate_python([{"sheet": "Data", "cell": "B2", "value": 9}]),
        APPENDS.validate_python([{"sheet": "Data", "rows": [["Ink", 1]]}]),
        "Budget v2",
    )
    assert env["success"], env
    assert (env["filename"], env["cells_updated"], env["rows_appended"]) == ("Budget v2.xlsx", 1, 1)
    assert isinstance(env["warnings"], list)
    assert store.get_content(att, OWNER_CTX) == original
    read = svc.read_xlsx(OWNER, env["attachment_id"], "Data", "B2", None)
    assert read["rows"] == [[9]]


def test_read_tools_mark_content_untrusted(svc, store):
    pptx = svc.read_pptx(OWNER, put(store, "deck.pptx", (FIXTURES / "sample.pptx").read_bytes()))
    xlsx = svc.read_xlsx(OWNER, put(store, "book.xlsx", (FIXTURES / "sample.xlsx").read_bytes()), None, None, None)
    assert pptx["content_trust"] == xlsx["content_trust"] == "untrusted"
    assert pptx["slide_count"] == 2


def test_create_pptx_and_xlsx(svc):
    deck = svc.create_pptx(OWNER, "deck", SLIDES.validate_python([{"layout": "section", "title": "Hi"}]))
    book = svc.create_xlsx(
        OWNER, "book", SHEETS.validate_python([{"name": "A", "columns": [{"header": "x", "type": "string"}]}])
    )
    assert (deck["filename"], book["filename"]) == ("deck.pptx", "book.xlsx")


def test_logs_never_contain_file_content_or_names(svc, store, caplog):
    caplog.set_level(logging.DEBUG)
    env = svc.create_docx(OWNER, "secret-name", BLOCKS.validate_python([{"type": "paragraph", "text": "SECRET-TEXT"}]))
    svc.read_docx(OWNER, env["attachment_id"], None)
    svc.read_docx(OTHER, env["attachment_id"], None)
    assert "SECRET-TEXT" not in caplog.text and "secret-name" not in caplog.text and "wi_1" not in caplog.text
    records = [r for r in caplog.records if r.name == "office_mcp"]
    assert {r.tool for r in records} == {"create_docx", "read_docx"}
    assert any(getattr(r, "error_code", None) == "PERMISSION_DENIED" for r in records)
