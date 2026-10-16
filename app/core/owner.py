"""Owner context injected by the gateway (proposed field ``owner_context_json``).

The agent never fills this. See docs/technical/PLATFORM_DEPENDENCIES.md (2.2 a).
"""

import json
from dataclasses import dataclass

from app.core import limits
from app.core.config import Settings
from app.core.exceptions import MSG_OWNER_CONTEXT_REQUIRED, ErrorCode, OfficeDocsError

OWNER_TYPES = ("work_item", "mcbp_run")


@dataclass(frozen=True)
class OwnerContext:
    owner_type: str
    owner_id: str
    tenant_id: str | None = None


DEV_OWNER = OwnerContext(owner_type="dev", owner_id="dev")


def _required() -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.OWNER_CONTEXT_REQUIRED, MSG_OWNER_CONTEXT_REQUIRED)


def parse_owner_context(raw: str | None, settings: Settings) -> OwnerContext:
    if raw is None or not raw.strip():
        if settings.allows_dev_owner:
            return DEV_OWNER
        raise _required()
    try:
        data = json.loads(raw)
    except ValueError:
        raise _required() from None
    if not isinstance(data, dict):
        raise _required()
    owner_type, owner_id, tenant_id = data.get("owner_type"), data.get("owner_id"), data.get("tenant_id")
    if owner_type not in OWNER_TYPES:
        raise _required()
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > limits.MAX_OWNER_ID_CHARS:
        raise _required()
    if tenant_id is not None and not isinstance(tenant_id, str):
        raise _required()
    return OwnerContext(owner_type=owner_type, owner_id=owner_id.strip(), tenant_id=(tenant_id or "").strip() or None)
