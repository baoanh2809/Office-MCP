"""End to end over HTTP: a real uvicorn server with the memory store, called like the gateway calls it."""

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.main import create_app
from app.mcp_server import set_service
from app.services.office_service import OfficeDocsService
from app.storage.memory_store import MemoryStore
from tests.test_service import SETTINGS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_registry_docs as gen  # noqa: E402

OWNER = json.dumps({"owner_type": "mcbp_run", "owner_id": "mcbp_wf1_run7", "tenant_id": "default"})
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    set_service(OfficeDocsService(SETTINGS, store=MemoryStore()))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield base
    srv.should_exit = True
    thread.join(timeout=5)
    set_service(None)


class Client:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, headers=HEADERS, timeout=30)
        self.session: str | None = None
        self.next_id = 0

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        headers = {"mcp-session-id": self.session} if self.session else {}
        resp = self.http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/json")
        if "mcp-session-id" in resp.headers:
            self.session = resp.headers["mcp-session-id"]
        return resp.json()

    def initialize(self) -> dict:
        res = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gateway-test", "version": "1"},
            },
        )
        self.http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"mcp-session-id": self.session},
        )
        return res

    def call(self, name: str, arguments: dict) -> dict:
        res = self.rpc("tools/call", {"name": name, "arguments": {**arguments, "owner_context_json": OWNER}})
        assert "result" in res, res
        assert res["result"]["isError"] is False
        return json.loads(res["result"]["content"][0]["text"])


def test_health(server):
    body = httpx.get(server + "/health").json()
    assert body == {"status": "ok", "service": "office-mcp", "storage": "memory", "storage_configured": True}


def test_initialize_and_tools_list(server):
    client = Client(server)
    init = client.initialize()
    assert init["result"]["serverInfo"]["name"] == "office-mcp"
    assert client.session
    tools = client.rpc("tools/list")["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(gen.TOOLS)
    for t in tools:
        schema = t["inputSchema"]
        assert schema["x-datumbridge-docs"].startswith(f"# {t['name']}\n")
        assert schema["x-datumbridge-capabilities"] == [f"office.{t['name']}"]
        assert "$ref" not in json.dumps(schema)


def test_create_read_update_read_xlsx(server):
    client = Client(server)
    client.initialize()
    created = client.call(
        "create_xlsx",
        {
            "filename": "Q3 sales",
            "sheets": [
                {
                    "name": "Sales",
                    "columns": [{"header": "Region", "type": "string"}, {"header": "Revenue", "type": "currency"}],
                    "rows": [["North", 120000], ["South", 95000], ["=cmd|' /C calc'!A0", 1]],
                }
            ],
        },
    )
    assert created["success"] and created["filename"] == "Q3 sales.xlsx"

    read = client.call("read_xlsx", {"attachment_id": created["attachment_id"]})
    assert read["content_trust"] == "untrusted"
    assert read["rows"] == [["Region", "Revenue"], ["North", 120000], ["South", 95000], ["=cmd|' /C calc'!A0", 1]]

    updated = client.call(
        "update_xlsx",
        {
            "attachment_id": created["attachment_id"],
            "updates": [{"sheet": "Sales", "cell": "B3", "value": 99000}],
            "append_rows": [{"sheet": "Sales", "rows": [["Total", {"formula": "=SUM(B2:B4)"}]]}],
        },
    )
    assert updated["success"] and updated["attachment_id"] != created["attachment_id"]
    assert updated["source_attachment_id"] == created["attachment_id"]

    reread = client.call("read_xlsx", {"attachment_id": updated["attachment_id"], "range": "A3:B5"})
    assert reread["rows"] == [
        ["South", 99000],
        ["=cmd|' /C calc'!A0", 1],
        ["Total", {"formula": "=SUM(B2:B4)", "value": None}],
    ]

    original = client.call("read_xlsx", {"attachment_id": created["attachment_id"], "range": "B3"})
    assert original["rows"] == [[95000]], "the source attachment must not change"


def test_missing_required_is_json_rpc_invalid_params(server):
    client = Client(server)
    client.initialize()
    missing_args, message = gen.TOOLS["create_xlsx"]["missing"]
    res = client.rpc("tools/call", {"name": "create_xlsx", "arguments": missing_args})
    assert res["error"] == {"code": -32602, "message": message}
    res = client.rpc(
        "tools/call",
        {"name": "create_pptx", "arguments": {"filename": "x", "slides": [{"layout": "blank", "title": "x"}]}},
    )
    assert res["error"]["code"] == -32602 and "slides.0.layout" in res["error"]["message"]


def test_owner_missing_is_an_envelope_not_a_protocol_error(server):
    client = Client(server)
    client.initialize()
    res = client.rpc("tools/call", {"name": "read_docx", "arguments": {"attachment_id": "att_1"}})
    body = json.loads(res["result"]["content"][0]["text"])
    assert body["error"]["error_code"] == "OWNER_CONTEXT_REQUIRED"
