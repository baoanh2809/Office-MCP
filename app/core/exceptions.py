"""Error codes: the single source of truth for handlers, registry_docs, and tests."""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    OWNER_CONTEXT_REQUIRED = "OWNER_CONTEXT_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    CORRUPT_FILE = "CORRUPT_FILE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    STORAGE_LIMIT_REACHED = "STORAGE_LIMIT_REACHED"
    STORAGE_NOT_CONFIGURED = "STORAGE_NOT_CONFIGURED"
    STORAGE_ERROR = "STORAGE_ERROR"


DOCUMENTED_ERROR_CODES: tuple[str, ...] = tuple(code.value for code in ErrorCode)

RETRYABLE_CODES: frozenset[ErrorCode] = frozenset({ErrorCode.STORAGE_ERROR})

# Fixed messages. registry_docs cases quote these, so change both together.
MSG_OWNER_CONTEXT_REQUIRED = "No Work Item or run context was attached to this call"
MSG_NOT_FOUND = "Attachment not found"
MSG_PERMISSION_DENIED = "Attachment is not available to this Work Item or run"
MSG_STORAGE_NOT_CONFIGURED = "Attachment storage is not configured on this server"
MSG_STORAGE_ERROR = "Attachment service is unavailable"
MSG_STORAGE_LIMIT_REACHED = "This Work Item or run already holds the maximum number of attachments"
MSG_OUTPUT_TOO_LARGE = "Generated file exceeds 20 MiB"
MSG_FILE_TOO_LARGE = "Attachment exceeds 20 MiB"
MSG_CORRUPT_FILE = "File could not be opened"
MSG_MACRO = "Files with macros are not supported"
MSG_LEGACY = "Legacy binary Office files (.doc, .xls, .ppt) are not supported"
MSG_INVALID_ATTACHMENT_ID = "attachment_id is not a valid attachment id"


class OfficeDocsError(Exception):
    """A handler failure that maps to the documented error envelope."""

    def __init__(self, code: ErrorCode, message: str, original_provider_error: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.original_provider_error = original_provider_error

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def validation_error(message: str) -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.VALIDATION_ERROR, message)


def corrupt_file(message: str = MSG_CORRUPT_FILE) -> OfficeDocsError:
    return OfficeDocsError(ErrorCode.CORRUPT_FILE, message)
