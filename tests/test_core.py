"""Error-code SSOT, envelopes, owner context, settings, and the read budget."""

import json

import pytest

from app.core import limits
from app.core.budget import TextBudget
from app.core.config import Settings
from app.core.exceptions import DOCUMENTED_ERROR_CODES, ErrorCode, OfficeDocsError
from app.core.owner import DEV_OWNER, OwnerContext, parse_owner_context
from app.schemas.envelopes import error_envelope, success_envelope

EXPECTED_CODES = {
    "VALIDATION_ERROR",
    "OWNER_CONTEXT_REQUIRED",
    "NOT_FOUND",
    "PERMISSION_DENIED",
    "UNSUPPORTED_FORMAT",
    "FILE_TOO_LARGE",
    "CORRUPT_FILE",
    "OUTPUT_TOO_LARGE",
    "STORAGE_LIMIT_REACHED",
    "STORAGE_NOT_CONFIGURED",
    "STORAGE_ERROR",
}


def test_documented_codes_are_exactly_the_contract_set():
    assert set(DOCUMENTED_ERROR_CODES) == EXPECTED_CODES
    assert len(DOCUMENTED_ERROR_CODES) == len(EXPECTED_CODES)


def test_only_storage_error_is_retryable():
    retryable = {c for c in ErrorCode if OfficeDocsError(c, "x").retryable}
    assert retryable == {ErrorCode.STORAGE_ERROR}


def test_error_envelope_shape():
    env = error_envelope(OfficeDocsError(ErrorCode.STORAGE_ERROR, "Attachment service is unavailable", "HTTP 503"))
    assert env == {
        "success": False,
        "error": {
            "error_code": "STORAGE_ERROR",
            "error_message": "Attachment service is unavailable",
            "retryable": True,
            "original_provider_error": "HTTP 503",
        },
    }


def test_success_envelope_puts_success_first():
    env = success_envelope(attachment_id="att_1", size_bytes=10)
    assert list(env) == ["success", "attachment_id", "size_bytes"]
    assert env["success"] is True


def _settings(storage="http", dev_mode=False) -> Settings:
    return Settings(storage=storage, dev_mode=dev_mode, local_dir=None, attachments_url=None, attachments_api_key=None)


def test_owner_context_parsed_from_injected_json():
    raw = json.dumps({"owner_type": "work_item", "owner_id": "wi_123", "tenant_id": "default"})
    assert parse_owner_context(raw, _settings()) == OwnerContext("work_item", "wi_123", "default")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "  ",
        "{}",
        "not json",
        "[]",
        json.dumps({"owner_type": "user", "owner_id": "u1"}),
        json.dumps({"owner_type": "work_item", "owner_id": ""}),
        json.dumps({"owner_type": "work_item", "owner_id": 5}),
        json.dumps({"owner_type": "mcbp_run", "owner_id": "r1", "tenant_id": 7}),
    ],
)
def test_missing_or_invalid_owner_context_is_rejected(raw):
    with pytest.raises(OfficeDocsError) as exc:
        parse_owner_context(raw, _settings())
    assert exc.value.code == ErrorCode.OWNER_CONTEXT_REQUIRED


@pytest.mark.parametrize("storage", ["local", "memory"])
def test_dev_mode_with_dev_storage_allows_missing_owner(storage):
    assert parse_owner_context(None, _settings(storage, dev_mode=True)) == DEV_OWNER


def test_dev_mode_never_relaxes_owner_for_http_storage():
    with pytest.raises(OfficeDocsError) as exc:
        parse_owner_context(None, _settings("http", dev_mode=True))
    assert exc.value.code == ErrorCode.OWNER_CONTEXT_REQUIRED


def test_settings_from_env(monkeypatch):
    for key in ("OFFICE_MCP_STORAGE", "OFFICE_MCP_DEV_MODE", "ATTACHMENTS_INTERNAL_URL"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert (s.storage, s.dev_mode) == ("http", False)
    monkeypatch.setenv("OFFICE_MCP_STORAGE", " Memory ")
    monkeypatch.setenv("OFFICE_MCP_DEV_MODE", "1")
    s = Settings.from_env()
    assert (s.storage, s.dev_mode) == ("memory", True)


def test_budget_cuts_at_character_boundary():
    b = TextBudget(5)
    assert b.take("ééééééé") == "ééééé"
    assert b.truncated and b.exhausted
    assert b.take("more") == ""


def test_budget_prefers_word_boundary():
    assert TextBudget(12).take("hello world again") == "hello world"


def test_budget_keeps_text_that_fits():
    b = TextBudget(10)
    assert (b.take("abc"), b.take("defg"), b.truncated, b.used) == ("abc", "defg", False, 7)


def test_budget_spend_is_all_or_nothing():
    b = TextBudget(5)
    assert b.spend(3) and not b.spend(3)
    assert b.truncated and b.used == 3


@pytest.mark.parametrize("value,expected", [(None, 100_000), (0, 100_000), (-5, 100_000), (10, 10), (10**9, 500_000)])
def test_clamp(value, expected):
    assert limits.clamp(value, limits.DEFAULT_MAX_CHARS, limits.MAX_MAX_CHARS) == expected
