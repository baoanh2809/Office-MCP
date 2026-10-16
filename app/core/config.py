"""Environment settings. Read once per process; tests build Settings directly."""

import os
from dataclasses import dataclass


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def _opt(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


@dataclass(frozen=True)
class Settings:
    storage: str  # http (default) | local | memory
    dev_mode: bool
    local_dir: str | None
    attachments_url: str | None
    attachments_api_key: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            storage=os.getenv("OFFICE_MCP_STORAGE", "").strip().lower() or "http",
            dev_mode=_flag("OFFICE_MCP_DEV_MODE"),
            local_dir=_opt("OFFICE_MCP_LOCAL_DIR"),
            attachments_url=_opt("ATTACHMENTS_INTERNAL_URL"),
            attachments_api_key=_opt("ATTACHMENTS_INTERNAL_API_KEY"),
        )

    @property
    def allows_dev_owner(self) -> bool:
        """Missing owner context falls back to the dev owner only for dev-mode local/memory storage."""
        return self.dev_mode and self.storage in ("local", "memory")
