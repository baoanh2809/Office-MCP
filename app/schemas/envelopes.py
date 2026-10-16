"""Success and error envelopes returned by every tool."""

from typing import Any

from app.core.exceptions import OfficeDocsError


def success_envelope(**fields: Any) -> dict[str, Any]:
    return {"success": True, **fields}


def error_envelope(err: OfficeDocsError) -> dict[str, Any]:
    return {
        "success": False,
        "error": {
            "error_code": err.code.value,
            "error_message": err.message,
            "retryable": err.retryable,
            "original_provider_error": err.original_provider_error,
        },
    }
