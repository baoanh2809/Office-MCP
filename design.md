# Design — office-mcp

## Class

**Tool-server** (Streamable HTTP MCP), not a WS hub relay. Standalone repo and image, like other MCP servers.

## Language

Python FastMCP (the platform default; see `Bright-Data-MCP/design.md`). `mcp==1.30.0` (`mcp.server.fastmcp`); 1.x
is pinned because 2.x renamed FastMCP to MCPServer. Office formats use `python-docx`, `openpyxl`, `python-pptx`.

## Upstream

- Attachment service (MongoDB GridFS) through the **proposed** internal API `/internal/v1/attachments` with
  `X-API-Key`. Files in and out are attachment ids; bytes never go to the agent.
- No vault, no OAuth, no `credentials_json`. The display name avoids `office365` / `office-365` / `microsoft-365`
  so `provider_match` never tries to inject Microsoft 365 tokens.

## Auth

- Service-to-service: `ATTACHMENTS_INTERNAL_API_KEY` sent as `X-API-Key`.
- Owner: the gateway injects `owner_context_json` (proposed) with the Work Item or MCBP run. The agent never fills
  it. Every read checks the attachment belongs to that owner.

## Tools

`create_docx`, `read_docx`, `edit_docx`, `create_xlsx`, `read_xlsx`, `update_xlsx`, `create_pptx`, `read_pptx`.

## Non-goals

PDF export, brand templates, editing .pptx, reading .doc/.xls/.ppt, macro-enabled files, storing files outside the
Attachment service.
