"""Bind the DatumBridge contract onto every FastMCP tool.

For each tool: ``x-datumbridge-capabilities`` and ``x-datumbridge-docs`` (registry_docs/<tool>.md, max 24 000
characters; longer fails instead of truncating), with ``$ref`` inlined so the registry stores a self-contained
schema. Also installs strict argument checking: a call whose arguments do not match the schema gets JSON-RPC
-32602 and the handler does not run.
"""

import copy
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

REGISTRY_DOCS_DIR = Path(__file__).resolve().parent.parent / "registry_docs"
DOCS_MAX_CHARS = 24_000


def registry_docs(name: str) -> str:
    text = (REGISTRY_DOCS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    if len(text) > DOCS_MAX_CHARS:
        raise ValueError(f"registry_docs/{name}.md is {len(text)} characters; the limit is {DOCS_MAX_CHARS}")
    return text


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace "#/$defs/X" references with copies of X and drop $defs and discriminator mappings."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                merged = {
                    **resolve(copy.deepcopy(defs[ref.split("/")[-1]])),
                    **{k: v for k, v in node.items() if k != "$ref"},
                }
                return resolve(merged)
            return {k: resolve(v) for k, v in node.items() if k not in ("$defs", "discriminator")}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def tool_capability(server_prefix: str, tool_name: str) -> str:
    return f"{server_prefix}.{tool_name}"


def bind(mcp: FastMCP, server_prefix: str) -> None:
    for tool in mcp._tool_manager.list_tools():
        params = inline_refs(tool.parameters)
        params["x-datumbridge-capabilities"] = [tool_capability(server_prefix, tool.name)]
        params["x-datumbridge-docs"] = registry_docs(tool.name)
        tool.parameters = params
    _install_strict_arguments(mcp)


def _summarize(err: ValidationError) -> str:
    parts = []
    for e in err.errors(include_input=False, include_url=False)[:5]:
        loc = ".".join(str(p) for p in e["loc"]) or "arguments"
        parts.append(f"{loc}: {e['msg']}")
    return "Invalid arguments: " + "; ".join(parts)


def _install_strict_arguments(mcp: FastMCP) -> None:
    lowlevel = mcp._mcp_server
    original = lowlevel.request_handlers[types.CallToolRequest]
    if getattr(original, "_strict_arguments", False):
        return

    async def handler(req: types.CallToolRequest):
        tool = mcp._tool_manager.get_tool(req.params.name)
        if tool is not None:
            arguments = dict(req.params.arguments or {})
            try:
                tool.fn_metadata.arg_model.model_validate(tool.fn_metadata.pre_parse_json(arguments))
            except ValidationError as err:
                raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message=_summarize(err))) from None
        return await original(req)

    handler._strict_arguments = True  # type: ignore[attr-defined]
    lowlevel.request_handlers[types.CallToolRequest] = handler
