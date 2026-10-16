"""MCP contract tests (mcp-tool-vibe-principles.md section 6).

Guides are checked against the code: tool names, schema properties, error codes, length, and the JSON
cases themselves, which are executed through the MCP layer and must match what the handler returns.
"""

import ast
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from app import capability_bind
from app.core.exceptions import DOCUMENTED_ERROR_CODES
from app.mcp_server import CAPABILITY_PREFIX, mcp, set_service
from app.services.office_service import OfficeDocsService
from app.storage.base import AttachmentMeta
from app.storage.memory_store import MemoryStore
from tests.test_service import SETTINGS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_registry_docs as gen  # noqa: E402

DOCS = ROOT / "registry_docs"
OWNER_JSON = json.dumps({"owner_type": "work_item", "owner_id": "wi_docs", "tenant_id": "default"})


def tool_names_from_ast() -> set[str]:
    tree = ast.parse((ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
    return names


TOOLS = sorted(tool_names_from_ast())


def guide(name: str) -> str:
    return (DOCS / f"{name}.md").read_text(encoding="utf-8")


def schemas() -> dict[str, dict]:
    return {t.name: t.inputSchema for t in asyncio.run(mcp.list_tools())}


def test_eight_tools_and_one_guide_each():
    assert TOOLS == sorted(
        ["create_docx", "read_docx", "edit_docx", "create_xlsx", "read_xlsx", "update_xlsx", "create_pptx", "read_pptx"]
    )
    assert {p.stem for p in DOCS.glob("*.md")} == set(TOOLS)


@pytest.mark.parametrize("name", TOOLS)
def test_guide_has_required_sections(name):
    text = guide(name)
    for needle in (
        f"# {name}\n",
        "## Agent rules",
        "## When to call",
        "## Parameters",
        "| Name | Required | Type | Sample | Related tool | Meaning |",
        "## Success fields",
        "## Error codes",
        "## Cases",
        "### Typical call",
        "### Missing required field",
        "-32602",
        "```json",
        "untrusted",
        "Do not invent a token",
    ):
        assert needle in text, f"{name}: missing {needle!r}"
    assert "invalid_argument" not in text
    assert len(text.strip()) <= capability_bind.DOCS_MAX_CHARS


def parameter_rows(text: str) -> dict[str, list[str]]:
    section = text.split("| Name | Required | Type | Sample | Related tool | Meaning |", 1)[1]
    table = section.split("\n\n", 1)[0]  # the parameter table ends at the first blank line
    rows = {}
    for line in table.splitlines():
        m = re.match(r"^\| `([a-z_]+)` \|(.*)\|$", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    return rows


@pytest.mark.parametrize("name", TOOLS)
def test_every_schema_property_has_a_row_with_a_sample(name):
    props = schemas()[name]["properties"]
    rows = parameter_rows(guide(name))
    assert set(rows) == set(props), name
    required = set(schemas()[name].get("required", []))
    for prop, cells in rows.items():
        req, _type, sample = cells[0], cells[1], cells[2]
        assert sample, f"{name}.{prop} has no sample"
        assert req == ("yes" if prop in required else "no"), f"{name}.{prop} required column"


@pytest.mark.parametrize("name", TOOLS)
def test_documented_codes_are_emitted_codes(name):
    codes = set(re.findall(r"^\| `([A-Z_]+)` \| (?:true|false) \|", guide(name), re.MULTILINE))
    assert codes and codes <= set(DOCUMENTED_ERROR_CODES)
    assert codes == set(gen.TOOLS[name]["codes"])


def test_every_ssot_code_is_documented_somewhere():
    documented = set()
    for name in TOOLS:
        documented |= set(re.findall(r"^\| `([A-Z_]+)` \| (?:true|false) \|", guide(name), re.MULTILINE))
    assert documented == set(DOCUMENTED_ERROR_CODES)


def test_retryable_column_matches_code():
    for name in TOOLS:
        for code, retry in re.findall(r"^\| `([A-Z_]+)` \| (true|false) \|", guide(name), re.MULTILINE):
            assert (retry == "true") == (code == "STORAGE_ERROR"), (name, code)


def test_generator_is_a_no_op():
    for name in TOOLS:
        assert gen.render(name, schemas()[name]) == guide(name), f"{name}: run scripts/generate_registry_docs.py"


def test_bind_puts_docs_and_capabilities_on_tools_list():
    capability_bind.bind(mcp, CAPABILITY_PREFIX)
    for tool in asyncio.run(mcp.list_tools()):
        schema = tool.inputSchema
        assert schema["x-datumbridge-docs"] == capability_bind.registry_docs(tool.name) == guide(tool.name).strip()
        assert schema["x-datumbridge-capabilities"] == [f"office.{tool.name}"]
        assert f"Capabilities: office.{tool.name}" in tool.description
        assert "\nOutputs: success" in tool.description
        assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)


def test_text_fields_declare_plain_encoding_and_closed_sets_use_enum():
    s = json.dumps(schemas())
    assert s.count('"x-datumbridge-encoding": "plain"') > 20
    for closed in ('"enum": [1, 2, 3, 4]', '"title_and_content"', '"currency"', '"replace_text"'):
        assert closed in s


def json_blocks(section: str) -> list:
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", section, re.DOTALL)]


def cases(name: str) -> list[tuple[str, list]]:
    body = guide(name).split("## Cases", 1)[1]
    parts = re.split(r"^### (.+)$", body, flags=re.MULTILINE)[1:]
    return [(parts[i].strip(), json_blocks(parts[i + 1])) for i in range(0, len(parts), 2)]


@pytest.mark.parametrize("name", TOOLS)
def test_typical_call_has_no_injected_fields_or_secrets(name):
    title, blocks = cases(name)[0]
    assert title == "Typical call"
    text = json.dumps(blocks[0]).lower()
    for forbidden in ("owner_context_json", "credentials_json", "api_key", "x-api-key", "token"):
        assert forbidden not in text


class SeededStore(MemoryStore):
    """Memory store that can hold attachments under the sample ids used in the guides."""

    def seed(self, attachment_id: str, filename: str, content_type: str, data: bytes) -> None:
        meta = AttachmentMeta(attachment_id, "work_item", "wi_docs", filename, content_type, len(data), "default")
        self._metas[attachment_id] = meta
        self._data[attachment_id] = data


@pytest.fixture
def seeded():
    store = SeededStore(max_files_per_owner=50)
    for attachment_id, (filename, content_type, data) in gen.sample_files().items():
        store.seed(attachment_id, filename, content_type, data)
    set_service(OfficeDocsService(SETTINGS, store=store))
    yield store
    set_service(None)


def call(name: str, args: dict, inject_owner: bool = True) -> dict:
    arguments = {**args, **({"owner_context_json": OWNER_JSON} if inject_owner else {})}
    return asyncio.run(mcp._tool_manager.call_tool(name, arguments, convert_result=False))


@pytest.mark.parametrize("name", TOOLS)
def test_documented_cases_match_the_handler(name, seeded):
    for title, blocks in cases(name):
        if title == "Missing required field":
            continue
        assert len(blocks) == 2, f"{name}: case {title!r} needs Input and Output JSON"
        doc_input, doc_output = blocks
        inject = "OWNER_CONTEXT_REQUIRED" not in json.dumps(doc_output)
        result = call(name, doc_input, inject_owner=inject)
        if doc_output.get("success") is False:
            assert result == doc_output, f"{name}: case {title!r}"
        else:
            assert result["success"] is True, (name, title, result)
            assert list(result) == list(doc_output), f"{name}: case {title!r} fields"
            for key in ("filename", "content_type", "truncated", "content_trust", "operations", "cells_updated"):
                if key in doc_output:
                    assert result[key] == doc_output[key], (name, title, key)


@pytest.mark.parametrize("name", TOOLS)
def test_success_fields_table_matches_typical_output(name):
    section = guide(name).split("## Success fields", 1)[1].split("\n## ", 1)[0]
    fields = re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE)
    typical_output = cases(name)[0][1][1]
    assert fields == list(typical_output)
