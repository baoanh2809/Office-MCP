"""Directory-backed attachment store: <dir>/<id>.bin + <id>.json. Dev mode only."""

import json
import re
import threading
from dataclasses import asdict
from pathlib import Path

from app.core.exceptions import MSG_STORAGE_ERROR, ErrorCode, OfficeDocsError
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

_LOCAL_ID = re.compile(r"^att_[0-9a-f]{24}$")


def _storage_error() -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.STORAGE_ERROR, MSG_STORAGE_ERROR)


class LocalStore(AttachmentStore):
    def __init__(self, directory: str, max_files_per_owner: int = DEFAULT_MAX_FILES_PER_OWNER) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_files = max_files_per_owner
        self._lock = threading.Lock()

    def _read_meta(self, attachment_id: str) -> AttachmentMeta:
        if not _LOCAL_ID.match(attachment_id):
            raise not_found()
        path = self._dir / f"{attachment_id}.json"
        if not path.exists():
            raise not_found()
        try:
            return AttachmentMeta(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            raise _storage_error() from None

    def get_metadata(self, attachment_id: str, owner: OwnerContext) -> AttachmentMeta:
        meta = self._read_meta(attachment_id)
        check_owner(meta, owner)
        return meta

    def get_content(self, attachment_id: str, owner: OwnerContext) -> bytes:
        self.get_metadata(attachment_id, owner)
        try:
            return (self._dir / f"{attachment_id}.bin").read_bytes()
        except OSError:
            raise _storage_error() from None

    def put(self, owner: OwnerContext, filename: str, content_type: str, data: bytes) -> AttachmentMeta:
        with self._lock:
            used = 0
            for path in self._dir.glob("att_*.json"):
                meta = self._read_meta(path.stem)
                if (meta.owner_type, meta.owner_id) == (owner.owner_type, owner.owner_id):
                    used += 1
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
            try:
                (self._dir / f"{meta.attachment_id}.bin").write_bytes(data)
                (self._dir / f"{meta.attachment_id}.json").write_text(json.dumps(asdict(meta)), encoding="utf-8")
            except OSError:
                raise _storage_error() from None
            return meta
