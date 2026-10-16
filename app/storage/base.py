"""AttachmentStore interface. Every read checks that the attachment belongs to the injected owner."""

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.exceptions import (
    MSG_NOT_FOUND,
    MSG_PERMISSION_DENIED,
    MSG_STORAGE_LIMIT_REACHED,
    ErrorCode,
    OfficeDocsError,
)
from app.core.owner import OwnerContext

DEFAULT_MAX_FILES_PER_OWNER = 5  # mirrors the Attachment service limit


@dataclass(frozen=True)
class AttachmentMeta:
    """Attachment metadata. ``filename`` is user data: never log it."""

    attachment_id: str
    owner_type: str
    owner_id: str
    filename: str
    content_type: str
    size: int
    tenant_id: str | None = None


class AttachmentStore(ABC):
    @abstractmethod
    def get_metadata(self, attachment_id: str, owner: OwnerContext) -> AttachmentMeta: ...

    @abstractmethod
    def get_content(self, attachment_id: str, owner: OwnerContext) -> bytes: ...

    @abstractmethod
    def put(self, owner: OwnerContext, filename: str, content_type: str, data: bytes) -> AttachmentMeta: ...


def check_owner(meta: AttachmentMeta, owner: OwnerContext) -> None:
    """PERMISSION_DENIED unless ``meta`` belongs to ``owner``. The message never describes the other owner."""
    same_owner = meta.owner_type == owner.owner_type and meta.owner_id == owner.owner_id
    same_tenant = not (owner.tenant_id and meta.tenant_id) or owner.tenant_id == meta.tenant_id
    if not (same_owner and same_tenant):
        raise OfficeDocsError(ErrorCode.PERMISSION_DENIED, MSG_PERMISSION_DENIED)


def not_found() -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.NOT_FOUND, MSG_NOT_FOUND)


def limit_reached() -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.STORAGE_LIMIT_REACHED, MSG_STORAGE_LIMIT_REACHED)


def new_attachment_id() -> str:
    return "att_" + secrets.token_hex(12)
