"""Tool logic: validate → owner → storage → safety checks → build/read/edit → upload → envelope.

Logs carry only tool, attachment_id, size_bytes, duration_ms, and error_code. Never file content, file
names, cell values, owner ids, or keys.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from app.builders.docx_builder import build_docx
from app.builders.pptx_builder import build_pptx
from app.builders.xlsx_builder import build_xlsx
from app.core import limits
from app.core.config import Settings
from app.core.exceptions import (
    MSG_FILE_TOO_LARGE,
    MSG_OUTPUT_TOO_LARGE,
    ErrorCode,
    OfficeDocsError,
    validation_error,
)
from app.core.owner import OwnerContext, parse_owner_context
from app.core.safety import (
    MIME,
    check_content_type,
    check_image,
    edited_filename,
    inspect_package,
    kind_from_filename,
    output_filename,
    validate_attachment_id,
)
from app.editors.docx_editor import edit_docx
from app.editors.xlsx_editor import update_xlsx
from app.readers.docx_reader import read_docx
from app.readers.pptx_reader import read_pptx
from app.readers.xlsx_reader import read_xlsx
from app.schemas.docx_spec import image_ids as docx_image_ids
from app.schemas.docx_spec import operation_image_ids, validate_blocks, validate_operations
from app.schemas.envelopes import error_envelope, success_envelope
from app.schemas.pptx_spec import image_ids as pptx_image_ids
from app.schemas.pptx_spec import validate_slides
from app.schemas.xlsx_spec import validate_sheets, validate_update
from app.storage.base import AttachmentMeta, AttachmentStore
from app.storage.factory import build_store

log = logging.getLogger("office_mcp")

UNTRUSTED = "untrusted"


class _Call:
    """Per-call log fields."""

    def __init__(self) -> None:
        self.attachment_id: str | None = None
        self.size_bytes: int | None = None


class OfficeDocsService:
    def __init__(
        self, settings: Settings, store: AttachmentStore | None = None, store_error: OfficeDocsError | None = None
    ) -> None:
        self.settings = settings
        self._store = store
        self._store_error = store_error

    @classmethod
    def from_settings(cls, settings: Settings) -> "OfficeDocsService":
        try:
            return cls(settings, store=build_store(settings))
        except OfficeDocsError as err:
            return cls(settings, store=None, store_error=err)

    @property
    def storage_configured(self) -> bool:
        return self._store is not None

    # --- plumbing -------------------------------------------------------------

    def _run(self, tool: str, fn: Callable[[_Call], dict[str, Any]]) -> dict[str, Any]:
        call = _Call()
        started = time.perf_counter()
        error_code = None
        try:
            result = fn(call)
        except OfficeDocsError as err:
            error_code = err.code.value
            result = error_envelope(err)
        except Exception as exc:  # a bug, not user input: never echo its message
            error_code = ErrorCode.VALIDATION_ERROR.value
            log.error("unexpected failure", extra={"tool": tool, "exception_type": type(exc).__name__})
            result = error_envelope(validation_error("The request could not be processed with these arguments"))
        log.info(
            "tool call",
            extra={
                "tool": tool,
                "attachment_id": call.attachment_id,
                "size_bytes": call.size_bytes,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "error_code": error_code,
            },
        )
        return result

    def _owner_and_store(self, owner_context_json: str | None) -> tuple[OwnerContext, AttachmentStore]:
        owner = parse_owner_context(owner_context_json, self.settings)
        if self._store is None:
            raise self._store_error or OfficeDocsError(ErrorCode.STORAGE_NOT_CONFIGURED, "storage not configured")
        return owner, self._store

    @staticmethod
    def _load(store, owner, attachment_id: str, kind: str, call: _Call) -> tuple[AttachmentMeta, bytes]:
        validate_attachment_id(attachment_id)
        call.attachment_id = attachment_id
        meta = store.get_metadata(attachment_id, owner)
        actual = kind_from_filename(meta.filename)
        if actual != kind:
            raise OfficeDocsError(
                ErrorCode.UNSUPPORTED_FORMAT, f"This tool reads .{kind} files; the attachment is a .{actual} file"
            )
        check_content_type(kind, meta.content_type)
        if meta.size > limits.MAX_INPUT_FILE_BYTES:
            raise OfficeDocsError(ErrorCode.FILE_TOO_LARGE, MSG_FILE_TOO_LARGE)
        data = store.get_content(attachment_id, owner)
        call.size_bytes = len(data)
        inspect_package(data, kind)
        return meta, data

    @staticmethod
    def _images(store, owner, ids: list[str]) -> dict:
        images = {}
        for image_id in ids:
            validate_attachment_id(image_id)
            store.get_metadata(image_id, owner)
            data = store.get_content(image_id, owner)
            images[image_id] = (data, check_image(data))
        return images

    @staticmethod
    def _save(store, owner, filename: str, kind: str, data: bytes, call: _Call) -> dict[str, Any]:
        if len(data) > limits.MAX_OUTPUT_FILE_BYTES:
            raise OfficeDocsError(ErrorCode.OUTPUT_TOO_LARGE, MSG_OUTPUT_TOO_LARGE)
        meta = store.put(owner, filename, MIME[kind], data)
        call.attachment_id = call.attachment_id or meta.attachment_id
        call.size_bytes = len(data)
        return success_envelope(
            attachment_id=meta.attachment_id,
            filename=meta.filename or filename,
            content_type=MIME[kind],
            size_bytes=len(data),
        )

    @staticmethod
    def _build(fn: Callable[[], bytes]) -> bytes:
        try:
            return fn()
        except OfficeDocsError:
            raise
        except Exception:
            raise validation_error("The file could not be built from this spec") from None

    # --- tools ----------------------------------------------------------------

    def create_docx(self, owner_context_json, filename, blocks) -> dict:
        def run(call: _Call):
            validate_blocks("blocks", blocks)
            name = output_filename(filename, "docx")
            owner, store = self._owner_and_store(owner_context_json)
            images = self._images(store, owner, docx_image_ids(blocks))
            return self._save(store, owner, name, "docx", self._build(lambda: build_docx(blocks, images)), call)

        return self._run("create_docx", run)

    def read_docx(self, owner_context_json, attachment_id, max_chars) -> dict:
        def run(call: _Call):
            owner, store = self._owner_and_store(owner_context_json)
            meta, data = self._load(store, owner, attachment_id, "docx", call)
            res = read_docx(data, max_chars)
            return success_envelope(
                attachment_id=attachment_id,
                filename=meta.filename,
                content_trust=UNTRUSTED,
                blocks=res.blocks,
                char_count=res.char_count,
                truncated=res.truncated,
            )

        return self._run("read_docx", run)

    def edit_docx(self, owner_context_json, attachment_id, operations, filename) -> dict:
        def run(call: _Call):
            validate_operations(operations)
            name = output_filename(filename, "docx") if filename else None
            owner, store = self._owner_and_store(owner_context_json)
            meta, data = self._load(store, owner, attachment_id, "docx", call)
            images = self._images(store, owner, operation_image_ids(operations))
            out, results = edit_docx(data, operations, images)
            env = self._save(store, owner, name or edited_filename(meta.filename, "docx"), "docx", out, call)
            return {**env, "source_attachment_id": attachment_id, "operations": results}

        return self._run("edit_docx", run)

    def create_xlsx(self, owner_context_json, filename, sheets) -> dict:
        def run(call: _Call):
            validate_sheets(sheets)
            name = output_filename(filename, "xlsx")
            owner, store = self._owner_and_store(owner_context_json)
            return self._save(store, owner, name, "xlsx", self._build(lambda: build_xlsx(sheets)), call)

        return self._run("create_xlsx", run)

    def read_xlsx(self, owner_context_json, attachment_id, sheet, range_ref, max_rows) -> dict:
        def run(call: _Call):
            owner, store = self._owner_and_store(owner_context_json)
            meta, data = self._load(store, owner, attachment_id, "xlsx", call)
            res = read_xlsx(data, sheet=sheet, range_ref=range_ref, max_rows=max_rows)
            return success_envelope(attachment_id=attachment_id, filename=meta.filename, content_trust=UNTRUSTED, **res)

        return self._run("read_xlsx", run)

    def update_xlsx(self, owner_context_json, attachment_id, updates, append_rows, filename) -> dict:
        def run(call: _Call):
            validate_update(updates or [], append_rows or [])
            name = output_filename(filename, "xlsx") if filename else None
            owner, store = self._owner_and_store(owner_context_json)
            meta, data = self._load(store, owner, attachment_id, "xlsx", call)
            out, res = update_xlsx(data, updates or [], append_rows or [])
            env = self._save(store, owner, name or edited_filename(meta.filename, "xlsx"), "xlsx", out, call)
            return {**env, "source_attachment_id": attachment_id, **res}

        return self._run("update_xlsx", run)

    def create_pptx(self, owner_context_json, filename, slides) -> dict:
        def run(call: _Call):
            validate_slides(slides)
            name = output_filename(filename, "pptx")
            owner, store = self._owner_and_store(owner_context_json)
            images = self._images(store, owner, pptx_image_ids(slides))
            return self._save(store, owner, name, "pptx", self._build(lambda: build_pptx(slides, images)), call)

        return self._run("create_pptx", run)

    def read_pptx(self, owner_context_json, attachment_id) -> dict:
        def run(call: _Call):
            owner, store = self._owner_and_store(owner_context_json)
            meta, data = self._load(store, owner, attachment_id, "pptx", call)
            res = read_pptx(data)
            return success_envelope(attachment_id=attachment_id, filename=meta.filename, content_trust=UNTRUSTED, **res)

        return self._run("read_pptx", run)
