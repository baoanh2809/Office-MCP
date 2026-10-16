"""Client for the PROPOSED internal Attachment endpoints (docs/technical/PLATFORM_DEPENDENCIES.md, 2.2 b).

    GET  /internal/v1/attachments/{id}?ownerType=&ownerId=          metadata (owner checked)
    GET  /internal/v1/attachments/{id}/content?ownerType=&ownerId=  raw bytes
    POST /internal/v1/attachments                                   multipart: file, ownerType, ownerId, origin

Every request sends ``X-API-Key``, like ``POST /internal/v1/inject`` in datumbridge-integrations. The tenant
goes in ``X-DatumBridge-Tenant-Id``, the header the platform honors only on the API-key path.
"""

from urllib.parse import quote

import httpx

from app.core import limits
from app.core.exceptions import (
    MSG_FILE_TOO_LARGE,
    MSG_NOT_FOUND,
    MSG_OUTPUT_TOO_LARGE,
    MSG_PERMISSION_DENIED,
    MSG_STORAGE_ERROR,
    MSG_STORAGE_LIMIT_REACHED,
    MSG_STORAGE_NOT_CONFIGURED,
    ErrorCode,
    OfficeDocsError,
)
from app.core.owner import OwnerContext
from app.storage.base import AttachmentMeta, AttachmentStore, check_owner

_PATH = "/internal/v1/attachments"


def _status_error(status: int) -> OfficeDocsError:
    """Map Attachment-service status to documented codes. Response bodies are never echoed."""
    provider = f"HTTP {status}"
    mapping = {
        401: (ErrorCode.STORAGE_NOT_CONFIGURED, MSG_STORAGE_NOT_CONFIGURED, provider),
        403: (ErrorCode.PERMISSION_DENIED, MSG_PERMISSION_DENIED, None),
        404: (ErrorCode.NOT_FOUND, MSG_NOT_FOUND, None),
        409: (ErrorCode.STORAGE_LIMIT_REACHED, MSG_STORAGE_LIMIT_REACHED, provider),
        413: (ErrorCode.OUTPUT_TOO_LARGE, MSG_OUTPUT_TOO_LARGE, provider),
        415: (ErrorCode.UNSUPPORTED_FORMAT, "Attachment service does not accept this file type", provider),
    }
    code, message, original = mapping.get(status, (ErrorCode.STORAGE_ERROR, MSG_STORAGE_ERROR, provider))
    return OfficeDocsError(code, message, original)


def _network_error() -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.STORAGE_ERROR, MSG_STORAGE_ERROR, "network error")


class HttpStore(AttachmentStore):
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=limits.STORAGE_TIMEOUT_SECONDS, follow_redirects=False)

    def _headers(self, owner: OwnerContext) -> dict[str, str]:
        headers = {"X-API-Key": self._api_key}
        if owner.tenant_id:
            headers["X-DatumBridge-Tenant-Id"] = owner.tenant_id
        return headers

    @staticmethod
    def _owner_query(owner: OwnerContext) -> dict[str, str]:
        return {"ownerType": owner.owner_type, "ownerId": owner.owner_id}

    def _item_url(self, attachment_id: str, suffix: str = "") -> str:
        return f"{self._base}{_PATH}/{quote(attachment_id, safe=':')}{suffix}"

    def get_metadata(self, attachment_id: str, owner: OwnerContext) -> AttachmentMeta:
        try:
            resp = self._client.get(
                self._item_url(attachment_id), params=self._owner_query(owner), headers=self._headers(owner)
            )
        except httpx.HTTPError:
            raise _network_error() from None
        if resp.status_code != 200:
            raise _status_error(resp.status_code)
        meta = _to_meta(resp)
        check_owner(meta, owner)  # defense in depth: the service checks owner too
        return meta

    def get_content(self, attachment_id: str, owner: OwnerContext) -> bytes:
        cap = limits.MAX_INPUT_FILE_BYTES
        try:
            with self._client.stream(
                "GET",
                self._item_url(attachment_id, "/content"),
                params=self._owner_query(owner),
                headers=self._headers(owner),
            ) as resp:
                if resp.status_code != 200:
                    raise _status_error(resp.status_code)
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > cap:
                        raise OfficeDocsError(ErrorCode.FILE_TOO_LARGE, MSG_FILE_TOO_LARGE)
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            raise _network_error() from None

    def put(self, owner: OwnerContext, filename: str, content_type: str, data: bytes) -> AttachmentMeta:
        try:
            resp = self._client.post(
                f"{self._base}{_PATH}",
                data={**self._owner_query(owner), "origin": "office-mcp"},
                files={"file": (filename, data, content_type)},
                headers=self._headers(owner),
            )
        except httpx.HTTPError:
            raise _network_error() from None
        if resp.status_code not in (200, 201):
            raise _status_error(resp.status_code)
        meta = _to_meta(resp)
        return AttachmentMeta(
            attachment_id=meta.attachment_id,
            owner_type=meta.owner_type or owner.owner_type,
            owner_id=meta.owner_id or owner.owner_id,
            filename=meta.filename or filename,
            content_type=meta.content_type or content_type,
            size=meta.size or len(data),
            tenant_id=meta.tenant_id or owner.tenant_id,
        )


def _to_meta(resp: httpx.Response) -> AttachmentMeta:
    try:
        body = resp.json()
        attachment_id = body["attachmentId"]
    except (ValueError, KeyError, TypeError):
        raise OfficeDocsError(ErrorCode.STORAGE_ERROR, MSG_STORAGE_ERROR, "invalid response") from None
    if not isinstance(attachment_id, str) or not attachment_id:
        raise OfficeDocsError(ErrorCode.STORAGE_ERROR, MSG_STORAGE_ERROR, "invalid response")
    return AttachmentMeta(
        attachment_id=attachment_id,
        owner_type=str(body.get("ownerType") or ""),
        owner_id=str(body.get("ownerId") or ""),
        filename=str(body.get("filename") or ""),
        content_type=str(body.get("contentType") or ""),
        size=int(body.get("size") or 0),
        tenant_id=body.get("tenantId") or None,
    )
