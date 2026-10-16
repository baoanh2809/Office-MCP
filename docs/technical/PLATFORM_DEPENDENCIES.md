# Platform dependencies

What office-mcp needs from the platform, what was verified, and what is still an assumption or a proposal.
Checked against `datumbridge-adk-design` @ `0ad2740` and `datumbridge-integrations` @ `c7bb10b` on 2026-10-16.

**None of the platform changes below are implemented.** This repo codes against the proposals behind interfaces
(`app/storage/base.py`, `app/core/owner.py`), so a different final contract changes one module.

## Summary

| # | Topic | Status | Blocks production? |
|---|---|---|---|
| a | Owner context injected by the gateway | **Proposal** (`owner_context_json`) | Yes |
| b | Internal Attachment API for services | **Proposal** (`/internal/v1/attachments`) | Yes |
| c | `pptx` in the Attachment allow-list | **Required change** (verified missing) | Only `create_pptx` uploads |
| d | 5-file limit counts generated files | **Assumption: yes** | No (returns `STORAGE_LIMIT_REACHED`) |
| e | Auto-delete of `mcbp_run` attachments | **Open question** | No |
| f | How the agent gets a user file's `attachment_id` | **Partly verified**, open questions | Yes for reading user files |
| g | Registry name and slug | Verified against `provider_match.go`; `ToSlug` not in workspace | No |

## a. Owner context

**Verified:** `mcp-tool-vibe-principles.md` §8 says the gateway sets `workspace_id` / `tenant_id` from the Work
Item, so a binding mechanism exists. No document names the field, and `datumbridge-integrations`
`POST /internal/v1/inject` only injects `credentials_json` from the vault.

**Proposal:** the gateway adds one string argument to every `tools/call` of this server:

```json
{"owner_context_json": "{\"owner_type\":\"work_item\",\"owner_id\":\"wi_8f2c\",\"tenant_id\":\"default\"}"}
```

- `owner_type`: `work_item` | `mcbp_run`; `owner_id`: Work Item id or `mcbp_{workflowId}_{runSession}`;
  `tenant_id`: JWT tenant.
- Same handling as `credentials_json`: present in the schema, guides tell the agent to omit it, the gateway
  overwrites whatever the model sent.
- Missing or malformed → `OWNER_CONTEXT_REQUIRED`. Only `OFFICE_MCP_DEV_MODE=1` with `local`/`memory` storage
  falls back to owner `dev`.

**Platform owner to confirm:** field name, and that the gateway overwrites (not merges) a model-supplied value.

## b. Internal Attachment API

**Verified:** the public API (`/design/attachments`) needs a user JWT with `runtime:execute`; an MCP server has
none. No internal service endpoint exists in the docs.

**Proposal** (modelled on `POST /internal/v1/inject`: `X-API-Key` checked against a shared secret, 401 otherwise).
Every call sends `X-API-Key` and, when known, `X-DatumBridge-Tenant-Id` (the header the platform already honors
only on the API-key path):

```http
GET /internal/v1/attachments/{attachmentId}?ownerType=work_item&ownerId=wi_8f2c
X-API-Key: <secret>
```

```json
{"attachmentId":"att_5d0e…","tenantId":"default","ownerType":"work_item","ownerId":"wi_8f2c",
 "filename":"Quarterly review.docx","contentType":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
 "size":12873,"status":"ready"}
```

```http
GET /internal/v1/attachments/{attachmentId}/content?ownerType=work_item&ownerId=wi_8f2c
X-API-Key: <secret>
→ 200 raw bytes
```

```http
POST /internal/v1/attachments
X-API-Key: <secret>
Content-Type: multipart/form-data
  file=<bytes; filename="Q3 summary.docx"; Content-Type: application/vnd…document>
  ownerType=work_item
  ownerId=wi_8f2c
  origin=office-mcp
→ 201 Attachment object (same shape as above)
```

Status contract the client relies on (`app/storage/http_store.py`):

| Status | Meaning | Tool error |
|---|---|---|
| 401 | Bad or missing service key | `STORAGE_NOT_CONFIGURED` |
| 403 | Attachment belongs to another owner/tenant | `PERMISSION_DENIED` |
| 404 | Unknown id | `NOT_FOUND` |
| 409 | Owner already holds the maximum number of files | `STORAGE_LIMIT_REACHED` |
| 413 | Upload too large | `OUTPUT_TOO_LARGE` |
| 415 | Type not in the allow-list | `UNSUPPORTED_FORMAT` |
| 5xx, network | Transient | `STORAGE_ERROR` (retryable) |

The client also re-checks `ownerType`/`ownerId`/`tenantId` on returned metadata, caps downloads at 20 MiB, and
never echoes response bodies.

**Trade-off to confirm:** the error table distinguishes `NOT_FOUND` from `PERMISSION_DENIED`, which tells a caller
that an id exists under another owner. Ids are random (`att_` + 24 hex), messages never describe the other owner
or file, and the agent only uses ids it was given. If the platform prefers, return 404 for both and the server
reports `NOT_FOUND`.

## c. `pptx` in the allow-list

**Verified missing:** `attachment-api-specification.md` allows pdf, docx, xlsx/xls, txt, md, csv, xml, png, jpeg,
svg. **Required change:** add
`application/vnd.openxmlformats-officedocument.presentationml.presentation` (`.pptx`). Until then `create_pptx`
gets 415 → `UNSUPPORTED_FORMAT`, and users cannot attach decks for `read_pptx`. The `create_pptx` guide tells the
agent this.

## d. 5 files per owner

**Verified:** the limit is 5 per owner. Whether generated files count is not specified. **Assumption:** they count
(safer). On the limit the service returns 409 → `STORAGE_LIMIT_REACHED`, and the guide tells the agent to stop and
ask the user to remove a file. **Proposal:** count files with `origin=office-mcp` separately, or raise the limit
for generated files; an edit-heavy conversation fills 5 slots fast.

## e. Auto-delete for `mcbp_run`

**Open question:** no retention policy is documented for `ownerType=mcbp_run`. Generated files may accumulate in
GridFS. Suggest a TTL for run attachments (or for `origin=office-mcp` files).

## f. Getting a user file's `attachment_id` into tool arguments

Read: `attachment-api-specification.md`, ADR-0081, ADR-0140, ADR-0142, vibe-principles "Related tool".

- **MCBP runs (verified):** run `input` carries `attachmentIds: string[]`. **Open:** ADR-0140 binding paths are
  `{{stepId.path}}` into earlier step outputs; the docs do not say whether a step can bind run input
  (`{{input.attachmentIds[0]}}`).
- **Work Items (gap):** Deep gets `attachmentsContext` (extracted Markdown, labelled untrusted). The docs do not say
  the attachment ids and file names are listed there. **Proposal:** include `attachmentId`, `filename`, and
  `contentType` per file in `attachmentsContext`, so the agent can pass the id to `read_*` / `edit_*`.
- **Between steps (verified):** `create_*` / `edit_*` / `update_*` return `attachment_id`; ADR-0140 prior-output
  binding can pass it to the next step.
- **Related tool:** deliberately `—`. Declaring `create_docx` as the producer of `attachment_id` would make Deep run
  a create tool just to obtain an id (ADR-0142 §4).

## g. Registry name

Register as **Office MCP** so the slug becomes `office-mcp-main`. **Verified** with a scratch copy of
`internal/credentials/provider_match.go`: `ResolveOAuthProvider("Office MCP",
"http://office-mcp-main.mcp-tools.svc.cluster.local:8000", "", nil)` returns `""` (no vault provider), while
the control `Microsoft 365 MCP` / `office365-mcp-main` still matches. `ToSlug` itself lives in a repo that is not in
the workspace, so the exact slug is **not verified**.

## Assumptions recorded while building

| Assumption | Safer choice taken |
|---|---|
| FastMCP package/version unknown (no sibling Python MCP in the workspace) | Official SDK `mcp==1.30.0`, pinned; 2.x renames FastMCP |
| Schema-invalid arguments must be JSON-RPC `-32602` (vibe-principles) | FastMCP 1.30 would return `isError` text; `capability_bind` installs a check that raises a real `-32602` |
| Gateway transport (verified only for Bright-Data-MCP: JSON responses, `Mcp-Session-Id`) | `json_response=True`, stateful sessions, path `/mcp` |
| Nested `$ref` support in the registry schema validator | Schemas are inlined (no `$defs`); discriminated unions become `oneOf` |
| A user file may contain prompt injection | Read results carry `content_trust: "untrusted"` (chosen over an `[UNTRUSTED_…]` text prefix because the result is JSON); every guide repeats the rule |
| Formulas written by the agent could carry untrusted text | Only `formula` creates formulas; `WEBSERVICE`, `CALL`, `REGISTER`, `EXEC`, `RTD`, `IMPORT*`, DDE refused |
| Host header checks | FastMCP DNS-rebinding protection is off when binding `0.0.0.0`; the Service is ClusterIP-only (no Ingress) |
