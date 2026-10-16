"""In-process attachment store. Tests and dev mode only."""

import threading

from app.core.owner import OwnerContext
from app.storage.base import (
    DEFAULT_MAX_FILES_PER_OWNER,
    AttachmentMeta,
    AttachmentStore,
    check_owner,
    limit_reached,
    new_attachment_id,
    not_found,
)


class MemoryStore(AttachmentStore):
    def __init__(self, max_files_per_owner: int = DEFAULT_MAX_FILES_PER_OWNER) -> None:
        self._max_files = max_files_per_owner
        self._lock = threading.Lock()
        self._metas: dict[str, AttachmentMeta] = {}
        self._data: dict[str, bytes] = {}

    def get_metadata(self, attachment_id: str, owner: OwnerContext) -> AttachmentMeta:
        with self._lock:
            meta = self._metas.get(attachment_id)
        if meta is None:
            raise not_found()
        check_owner(meta, owner)
        return meta

    def get_content(self, attachment_id: str, owner: OwnerContext) -> bytes:
        self.get_metadata(attachment_id, owner)
        with self._lock:
            return self._data[attachment_id]

    def put(self, owner: OwnerContext, filename: str, content_type: str, data: bytes) -> AttachmentMeta:
        with self._lock:
            used = sum(
                1 for m in self._metas.values() if (m.owner_type, m.owner_id) == (owner.owner_type, owner.owner_id)
            )
            if used >= self._max_files:
                raise limit_reached()
            meta = AttachmentMeta(
                attachment_id=new_attachment_id(),
                owner_type=owner.owner_type,
                owner_id=owner.owner_id,
                filename=filename,
                content_type=content_type,
                size=len(data),
                tenant_id=owner.tenant_id,
            )
            self._metas[meta.attachment_id] = meta
            self._data[meta.attachment_id] = bytes(data)
            return meta
