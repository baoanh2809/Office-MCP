# office-mcp

MCP server that lets Weaver agents **create** Word, Excel, and PowerPoint files and **read or edit** files users
attached to a Work Item or MCBP run. Files move as Attachment-service ids; the agent never sees file bytes, and an
edit always produces a new attachment.

- Registry display name: **Office MCP** (`mcpServer` id `office-mcp`)
- Transport: Streamable HTTP at `POST /mcp` (JSON responses, `Mcp-Session-Id` sessions), `GET /health`
- Design: [`design.md`](design.md) · Tool contract: [`docs/technical/MCP_TOOL_SPECIFICATION.md`](docs/technical/MCP_TOOL_SPECIFICATION.md)
- **Platform changes needed before production:** [`docs/technical/PLATFORM_DEPENDENCIES.md`](docs/technical/PLATFORM_DEPENDENCIES.md)

## Tools

| Tool | Does | Returns |
|---|---|---|
| `create_docx` | New .docx from blocks: heading, paragraph, bullet/numbered list, table, page break, image | new `attachment_id` |
| `read_docx` | .docx → ordered blocks | blocks, `truncated` |
| `edit_docx` | `replace_text`, `append_blocks` | new `attachment_id` |
| `create_xlsx` | New .xlsx with typed columns, frozen header, autofilter | new `attachment_id` |
| `read_xlsx` | Sheet list + values of one range (formula + cached value) | rows, `truncated` |
| `update_xlsx` | Set cells, append rows | new `attachment_id`, `warnings` |
| `create_pptx` | New 16:9 deck: title, section, bullets, two columns, table, image, notes | new `attachment_id` |
| `read_pptx` | Per slide: title, text, tables, notes | slides, `truncated` |

Each tool's Deep Agent guide is `registry_docs/<tool>.md`; the server publishes it as `x-datumbridge-docs`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Listen port |
| `HOST` | `0.0.0.0` | Listen address |
| `LOG_LEVEL` | `INFO` | Log level (JSON lines on stdout) |
| `OFFICE_MCP_STORAGE` | `http` | `http`, `local`, or `memory` |
| `ATTACHMENTS_INTERNAL_URL` | — | Base URL of the internal Attachment API (`http` storage) |
| `ATTACHMENTS_INTERNAL_API_KEY` | — | Sent as `X-API-Key`. Keep it in a Secret |
| `OFFICE_MCP_DEV_MODE` | off | `1` allows `local`/`memory` storage and calls without owner context (owner `dev`) |
| `OFFICE_MCP_LOCAL_DIR` | — | Directory for `local` storage |

With `http` storage and a missing URL or key, `/health` still answers `ok` (`storage_configured: false`) and tools
return `STORAGE_NOT_CONFIGURED`.

## Develop

```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check .
pytest
python scripts/generate_registry_docs.py          # rewrite registry_docs from the code
python scripts/generate_registry_docs.py --check  # fail if a guide is stale
python scripts/make_fixtures.py                   # rebuild tests/fixtures (raw-XML reader fixtures)
```

Run locally without the Attachment service:

```powershell
$env:OFFICE_MCP_STORAGE = "memory"; $env:OFFICE_MCP_DEV_MODE = "1"
python -m app.main
```

Docker: `docker build -t office-mcp:dev .` (non-root uid 1000, works with `readOnlyRootFilesystem`).

## Safety

- Input files: zip signature, matching content type, no `vbaProject.bin` or macro content types, no DTDs, at most
  10 000 entries, 200 MiB expanded, compression ratio 100, 20 MiB per file. Legacy and macro formats are refused.
- Spreadsheet text is always written as text; only an explicit `formula` creates a formula, and external-call
  functions and DDE are refused.
- Read results carry `content_trust: "untrusted"`; guides tell the agent not to follow instructions in files.
- Logs hold only tool, attachment id, size, duration, and error code.

## Repository layout

```text
app/            main.py, mcp_server.py, capability_bind.py, core/, schemas/, builders/, readers/, editors/,
                storage/, services/
registry_docs/  Deep Agent guide per tool (generated)
scripts/        generate_registry_docs.py, make_fixtures.py
tests/          unit, contract, and HTTP end-to-end tests; fixtures/
docs/           technical/PLATFORM_DEPENDENCIES.md, technical/MCP_TOOL_SPECIFICATION.md, changelog.md
```
