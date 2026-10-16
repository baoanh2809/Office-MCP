# MCP tool specification

The per-tool Deep Agent guides in `registry_docs/` are the contract the agent sees (parameters with samples,
success fields, error codes, executed cases). This page is the server-side reference.

## Transport

| Item | Value |
|---|---|
| MCP endpoint | `POST /mcp` (Streamable HTTP, `application/json` responses, `Mcp-Session-Id` after `initialize`) |
| Health | `GET /health` → `{"status":"ok","service":"office-mcp","storage":"http","storage_configured":true}` |
| Port | `PORT`, default 8000 |
| Request body cap | 32 MiB |

Each tool's input schema carries `x-datumbridge-capabilities` (`office.<tool>`) and `x-datumbridge-docs`
(the guide). Descriptions end with `Capabilities:` and `Outputs:` lines. Free-text fields declare
`x-datumbridge-encoding: plain`; closed sets (block type, heading level, column type, layout, operation type) are
`enum`/`const`. `$ref`s are inlined.

## Responses

- Success: `{"success": true, ...fields}`. Writes return `attachment_id`, `filename`, `content_type`, `size_bytes`
  (edits also `source_attachment_id`). Reads return `content_trust: "untrusted"` and `truncated`.
- Handler failure: `{"success": false, "error": {"error_code", "error_message", "retryable", "original_provider_error"}}`.
- Arguments that do not match the schema: JSON-RPC error `-32602`, handler not run.

## Error codes (`app/core/exceptions.py`)

| `error_code` | retryable | Raised when |
|---|---|---|
| `VALIDATION_ERROR` | false | Limits and rules beyond the schema (counts, text, sheet names, ranges, formulas, layout fields, filenames) |
| `OWNER_CONTEXT_REQUIRED` | false | No valid `owner_context_json` |
| `NOT_FOUND` | false | Unknown attachment id |
| `PERMISSION_DENIED` | false | Attachment of another owner/tenant, or Attachment service 403 |
| `UNSUPPORTED_FORMAT` | false | Wrong type for the tool, legacy or macro file, mismatched content type, image not PNG/JPEG, service 415 |
| `FILE_TOO_LARGE` | false | Input over 20 MiB; image over 10 MiB or 40 MP |
| `CORRUPT_FILE` | false | Not a valid package, zip-bomb limits, DTD, library could not open it |
| `OUTPUT_TOO_LARGE` | false | Generated file over 20 MiB (not uploaded), or service 413 |
| `STORAGE_LIMIT_REACHED` | false | Owner file limit (service 409) |
| `STORAGE_NOT_CONFIGURED` | false | Storage env missing, dev storage without dev mode, service 401 |
| `STORAGE_ERROR` | true | Network error or 5xx from the Attachment service |

An unexpected exception inside a handler (a bug) is logged by type only and returned as `VALIDATION_ERROR` with a
fixed message, so library messages that may quote file content never reach the agent.

## Processing order

`validate spec → validate filename → owner context → storage → fetch + check inputs → build/read/edit → size check
→ upload → envelope`. Validation errors are returned before any storage call.

## Limits (`app/core/limits.py`)

| Limit | Value |
|---|---|
| Input file / output file | 20 MiB / 20 MiB |
| Image | 10 MiB, 40 000 000 pixels, PNG or JPEG |
| Zip package | ≤ 10 000 entries, ≤ 200 MiB expanded, compression ratio ≤ 100 |
| Blocks per create / per edit | 2 000 |
| Slides | 200 (bullets 50, table 20 × 50) |
| Workbook cells written per call | 1 000 000 (headers included) |
| Read text | default 100 000, max 500 000 characters |
| `read_xlsx` rows | default 200, max 5 000 |
| Edit operations / cell updates | 500 / 10 000 |

## Format behavior and known limits

- **create_docx:** A4, 2.54 cm margins, python-docx default styles (Heading 1–4, List Paragraph, Table Grid). Each
  numbered list restarts at 1. Images default to natural size at 96 dpi, capped at the text width.
- **read_docx:** body only, in order. Includes hyperlinks, content controls, tracked insertions. Excludes headers,
  footers, footnotes, comments, text boxes, tracked deletions, `mc:Fallback` copies. Lists are grouped by numbering
  id; a list is ordered unless its level format is `bullet`/`none`.
- **edit_docx `replace_text`:** body, headers, footers, and text boxes, paragraph by paragraph. A match may span
  runs; the replacement goes into the first run (its formatting) and the other runs lose only the matched
  characters. Matches never cross tabs, breaks, or drawings. Deleted text is not searched. Matches in
  `mc:Fallback` copies are replaced but not counted. `find`/`replace` are single-line.
- **edit_docx `append_blocks`:** before the final section properties. Styles are looked up by name in the target
  document (so localized ids such as `Titre1` are reused); missing styles fall back to direct formatting. New
  numbering definitions get ids after the existing ones; a numbering part is created when absent.
- **create_xlsx:** write-only streaming. String cells are always text (`data_type = "s"`); formulas only from
  `{"formula": …}`. Header row bold with fill, frozen, autofilter over the data; widths from content (8–60);
  `fullCalcOnLoad`.
- **read_xlsx:** read-only streaming, opened twice (formulas and cached values). Cached value is `null` when Excel
  never recalculated the file. Date-formatted numbers → ISO strings. A row stops at the first cell that does not fit
  the text budget (no null padding).
- **update_xlsx:** openpyxl rewrites the workbook: values, formulas, styles, merged cells, validation, and
  conditional formatting stay; charts, images, pivot tables, and cached formula values are dropped and reported in
  `warnings`. `fullCalcOnLoad` is set.
- **create_pptx:** python-pptx default template widened to 16:9 (master and layout placeholders rescaled). Unused
  placeholders are removed. Table and image slides use the Title Only layout.
- **read_pptx:** slide order from `sldIdLst`; group shapes walked; slide number/date/footer/header placeholders
  skipped; notes from the notes body placeholder.

## Storage modes

| `OFFICE_MCP_STORAGE` | Use |
|---|---|
| `http` (default) | Production. Needs `ATTACHMENTS_INTERNAL_URL` and `ATTACHMENTS_INTERNAL_API_KEY` |
| `local` | Dev only (`OFFICE_MCP_DEV_MODE=1`), files in `OFFICE_MCP_LOCAL_DIR` |
| `memory` | Tests and dev only (`OFFICE_MCP_DEV_MODE=1`) |

Processing is in memory (`io.BytesIO`); nothing is written to disk in `http` mode.

## Logging

JSON lines with `tool`, `attachment_id`, `size_bytes`, `duration_ms`, `error_code` (plus `exception_type` for
bugs). Never file content, file names, cell values, owner ids, or keys. httpx/MCP transport loggers are at WARNING.
