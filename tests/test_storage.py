"""Attachment storage: owner checks, limits, the proposed internal HTTP API, and configuration."""

import json
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from app.core import limits
from app.core.config import Settings
from app.core.exceptions import ErrorCode, OfficeDocsError
from app.core.owner import OwnerContext
from app.core.safety import MIME
from app.storage.factory import build_store
from app.storage.http_store import HttpStore
from app.storage.local_store import LocalStore
from app.storage.memory_store import MemoryStore

OWNER_A = OwnerContext("work_item", "wi_a", "default")
OWNER_B = OwnerContext("work_item", "wi_b", "default")


def _code(fn, *args) -> ErrorCode:
    with pytest.raises(OfficeDocsError) as exc:
        fn(*args)
    return exc.value.code


def exercise(store) -> None:
    meta = store.put(OWNER_A, "report.docx", MIME["docx"], b"hello")
    assert meta.attachment_id and meta.filename == "report.docx" and meta.size == 5
    assert (meta.owner_type, meta.owner_id) == ("work_item", "wi_a")
    assert store.get_metadata(meta.attachment_id, OWNER_A).content_type == MIME["docx"]
    assert store.get_content(meta.attachment_id, OWNER_A) == b"hello"
    assert _code(store.get_metadata, meta.attachment_id, OWNER_B) == ErrorCode.PERMISSION_DENIED
    assert _code(store.get_content, meta.attachment_id, OWNER_B) == ErrorCode.PERMISSION_DENIED
    assert _code(store.get_metadata, "att_missing", OWNER_A) == ErrorCode.NOT_FOUND
    second = store.put(OWNER_A, "copy.docx", MIME["docx"], b"x")
    assert second.attachment_id != meta.attachment_id


def test_memory_store():
    exercise(MemoryStore())


def test_memory_store_owner_limit():
    store = MemoryStore(max_files_per_owner=2)
    store.put(OWNER_A, "a.docx", "", b"x")
    store.put(OWNER_A, "a.docx", "", b"x")
    assert _code(store.put, OWNER_A, "a.docx", "", b"x") == ErrorCode.STORAGE_LIMIT_REACHED
    store.put(OWNER_B, "a.docx", "", b"x")  # other owners still have room


def test_local_store(tmp_path):
    store = LocalStore(str(tmp_path))
    exercise(store)
    meta = store.put(OWNER_B, "b.xlsx", MIME["xlsx"], b"bytes")
    assert LocalStore(str(tmp_path)).get_content(meta.attachment_id, OWNER_B) == b"bytes"
    assert _code(store.get_metadata, "../../etc/passwd", OWNER_A) == ErrorCode.NOT_FOUND


def _settings(**kw) -> Settings:
    base = {
        "storage": "http",
        "dev_mode": False,
        "local_dir": None,
        "attachments_url": None,
        "attachments_api_key": None,
    }
    return Settings(**{**base, **kw})


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"attachments_url": "http://x"},
        {"storage": "local", "local_dir": "x"},
        {"storage": "local", "dev_mode": True},
        {"storage": "memory"},
        {"storage": "s3", "dev_mode": True},
    ],
)
def test_misconfigured_storage(kw):
    assert _code(build_store, _settings(**kw)) == ErrorCode.STORAGE_NOT_CONFIGURED


def test_configured_storage(tmp_path):
    assert isinstance(build_store(_settings(attachments_url="http://x", attachments_api_key="k")), HttpStore)
    assert isinstance(build_store(_settings(storage="local", dev_mode=True, local_dir=str(tmp_path))), LocalStore)
    assert isinstance(build_store(_settings(storage="memory", dev_mode=True)), MemoryStore)


# --- fake Attachment service ---------------------------------------------------


class FakeAttachments:
    """In-process Attachment service implementing the proposed internal API."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.force_status: int | None = None
        self.owner_cap = 5
        self.raw_handler = None

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(h.path)
        self.requests.append((h.command, parsed.path, h.headers.get("X-API-Key")))
        # Always drain the request body so an early response does not reset the connection.
        body = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        if self.raw_handler:
            return self.raw_handler(h)
        if self.force_status:
            return self._send(h, self.force_status, b'{"error":"forced"}')
        if h.headers.get("X-API-Key") != "secret-key":
            return self._send(h, 401, b"")
        prefix = "/internal/v1/attachments"
        if h.command == "POST" and parsed.path == prefix:
            msg = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + h.headers["Content-Type"].encode() + b"\r\n\r\n" + body
            )
            fields, file = {}, None
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "file":
                    file = part
                else:
                    fields[name] = part.get_content().strip()
            if sum(1 for f in self.files.values() if f["meta"]["ownerId"] == fields["ownerId"]) >= self.owner_cap:
                return self._send(h, 409, b"")
            data = file.get_payload(decode=True)
            att_id = f"att_fake{len(self.files) + 1}"
            meta = {
                "attachmentId": att_id,
                "tenantId": h.headers.get("X-DatumBridge-Tenant-Id"),
                "ownerType": fields["ownerType"],
                "ownerId": fields["ownerId"],
                "filename": file.get_filename(),
                "contentType": file.get_content_type(),
                "size": len(data),
                "origin": fields.get("origin"),
            }
            self.files[att_id] = {"meta": meta, "data": data}
            return self._send(h, 201, json.dumps(meta).encode())
        if h.command == "GET" and parsed.path.startswith(prefix + "/"):
            rest = parsed.path[len(prefix) + 1 :]
            att_id, content = rest.removesuffix("/content"), rest.endswith("/content")
            f = self.files.get(att_id)
            if f is None:
                return self._send(h, 404, b"")
            q = parse_qs(parsed.query)
            if q.get("ownerType") != [f["meta"]["ownerType"]] or q.get("ownerId") != [f["meta"]["ownerId"]]:
                return self._send(h, 403, b"")
            return self._send(h, 200, f["data"] if content else json.dumps(f["meta"]).encode())
        return self._send(h, 404, b"")

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
        h.send_response(status)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)


@pytest.fixture
def fake():
    service = FakeAttachments()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            service.handle(self)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service.url = f"http://127.0.0.1:{server.server_port}/"
    yield service
    server.shutdown()
    server.server_close()


def _store(fake) -> HttpStore:
    return HttpStore(fake.url, "secret-key")


def test_http_store_against_fake_service(fake):
    exercise(_store(fake))
    assert all(key == "secret-key" for _, _, key in fake.requests)
    calls = {(m, p) for m, p, _ in fake.requests}
    assert {
        ("POST", "/internal/v1/attachments"),
        ("GET", "/internal/v1/attachments/att_fake1"),
        ("GET", "/internal/v1/attachments/att_fake1/content"),
    } <= calls
    meta = fake.files["att_fake1"]["meta"]
    assert meta["tenantId"] == "default" and meta["origin"] == "office-mcp"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.STORAGE_NOT_CONFIGURED),
        (403, ErrorCode.PERMISSION_DENIED),
        (404, ErrorCode.NOT_FOUND),
        (409, ErrorCode.STORAGE_LIMIT_REACHED),
        (413, ErrorCode.OUTPUT_TOO_LARGE),
        (415, ErrorCode.UNSUPPORTED_FORMAT),
        (500, ErrorCode.STORAGE_ERROR),
        (502, ErrorCode.STORAGE_ERROR),
        (503, ErrorCode.STORAGE_ERROR),
    ],
)
def test_http_status_mapping(fake, status, code):
    fake.force_status = status
    assert _code(_store(fake).put, OWNER_A, "a.docx", MIME["docx"], b"x") == code


def test_http_5xx_is_retryable_storage_error(fake):
    fake.force_status = 503
    with pytest.raises(OfficeDocsError) as exc:
        _store(fake).get_metadata("att_1", OWNER_A)
    err = exc.value
    assert (err.code, err.retryable, err.original_provider_error) == (ErrorCode.STORAGE_ERROR, True, "HTTP 503")


def test_http_network_failure_is_storage_error():
    store = HttpStore("http://127.0.0.1:9/", "k")  # discard port: connection refused
    assert _code(store.get_metadata, "att_1", OWNER_A) == ErrorCode.STORAGE_ERROR


def test_http_upload_limit(fake):
    fake.owner_cap = 1
    store = _store(fake)
    store.put(OWNER_A, "a.docx", "", b"x")
    assert _code(store.put, OWNER_A, "b.docx", "", b"x") == ErrorCode.STORAGE_LIMIT_REACHED


def test_http_metadata_for_another_owner_is_denied(fake):
    # A service that ignores the owner query must still not leak another owner's file.
    fake.raw_handler = lambda h: FakeAttachments._send(
        h, 200, json.dumps({"attachmentId": "att_1", "ownerType": "work_item", "ownerId": "wi_other"}).encode()
    )
    assert _code(_store(fake).get_metadata, "att_1", OWNER_A) == ErrorCode.PERMISSION_DENIED


def test_http_content_size_capped(fake, monkeypatch):
    monkeypatch.setattr(limits, "MAX_INPUT_FILE_BYTES", 4)
    fake.raw_handler = lambda h: FakeAttachments._send(h, 200, b"0123456789")
    assert _code(_store(fake).get_content, "att_1", OWNER_A) == ErrorCode.FILE_TOO_LARGE


def test_http_escapes_id(fake):
    fake.force_status = 404
    with pytest.raises(OfficeDocsError):
        _store(fake).get_metadata("att:1", OWNER_A)
    assert fake.requests[-1][1] == "/internal/v1/attachments/att:1"
