"""Smoke test a running server over HTTP: health, initialize, tools/list, and an xlsx create -> read -> update -> read.

    python scripts/smoke_mcp.py http://127.0.0.1:8000 [owner_context_json]

Against a dev server (OFFICE_MCP_STORAGE=memory, OFFICE_MCP_DEV_MODE=1) the owner argument may be omitted.
Prints only status lines, never file content.
"""

import json
import sys

import httpx

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def main(base: str, owner: str) -> int:
    http = httpx.Client(base_url=base.rstrip("/"), headers=HEADERS, timeout=30)
    health = http.get("/health").json()
    print("health:", health["status"], "storage_configured:", health["storage_configured"])

    resp = http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"},
            },
        },
    )
    session = {"mcp-session-id": resp.headers["mcp-session-id"]}
    http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)

    seq = iter(range(2, 1000))

    def rpc(method: str, params: dict) -> dict:
        return http.post(
            "/mcp", json={"jsonrpc": "2.0", "id": next(seq), "method": method, "params": params}, headers=session
        ).json()

    tools = rpc("tools/list", {})["result"]["tools"]
    with_docs = sum(1 for t in tools if t["inputSchema"].get("x-datumbridge-docs"))
    print(f"tools/list: {len(tools)} tools, {with_docs} with x-datumbridge-docs")

    def call(name: str, arguments: dict) -> dict:
        if owner:
            arguments = {**arguments, "owner_context_json": owner}
        body = json.loads(rpc("tools/call", {"name": name, "arguments": arguments})["result"]["content"][0]["text"])
        status = "ok" if body["success"] else body["error"]["error_code"]
        print(f"{name}: {status}")
        if not body["success"]:
            raise SystemExit(1)
        return body

    created = call(
        "create_xlsx",
        {
            "filename": "smoke",
            "sheets": [
                {
                    "name": "S",
                    "columns": [{"header": "k", "type": "string"}, {"header": "v", "type": "number"}],
                    "rows": [["a", 1]],
                }
            ],
        },
    )
    first = call("read_xlsx", {"attachment_id": created["attachment_id"]})
    updated = call(
        "update_xlsx",
        {"attachment_id": created["attachment_id"], "updates": [{"sheet": "S", "cell": "B2", "value": 2}]},
    )
    second = call("read_xlsx", {"attachment_id": updated["attachment_id"]})
    ok = first["rows"][1][1] == 1 and second["rows"][1][1] == 2 and updated["attachment_id"] != created["attachment_id"]
    print("end-to-end:", "ok" if ok else "MISMATCH")

    missing = rpc("tools/call", {"name": "read_xlsx", "arguments": {}})
    print("missing argument -> JSON-RPC", missing.get("error", {}).get("code"))
    return 0 if ok and len(tools) == 8 and with_docs == 8 and missing.get("error", {}).get("code") == -32602 else 1


if __name__ == "__main__":
    sys.exit(
        main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000", sys.argv[2] if len(sys.argv) > 2 else "")
    )
