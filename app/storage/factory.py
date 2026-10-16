"""Build the store selected by OFFICE_MCP_STORAGE. Misconfiguration raises STORAGE_NOT_CONFIGURED."""

from app.core.config import Settings
from app.core.exceptions import MSG_STORAGE_NOT_CONFIGURED, ErrorCode, OfficeDocsError
from app.storage.base import AttachmentStore
from app.storage.http_store import HttpStore
from app.storage.local_store import LocalStore
from app.storage.memory_store import MemoryStore


def build_store(settings: Settings) -> AttachmentStore:
    not_configured = OfficeDocsError(ErrorCode.STORAGE_NOT_CONFIGURED, MSG_STORAGE_NOT_CONFIGURED)
    if settings.storage == "http":
        if not settings.attachments_url or not settings.attachments_api_key:
            raise not_configured
        return HttpStore(settings.attachments_url, settings.attachments_api_key)
    if settings.storage == "local":
        if not settings.dev_mode or not settings.local_dir:
            raise not_configured
        return LocalStore(settings.local_dir)
    if settings.storage == "memory":
        if not settings.dev_mode:
            raise not_configured
        return MemoryStore()
    raise not_configured
