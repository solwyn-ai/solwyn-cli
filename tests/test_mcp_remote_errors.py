"""MCP tools carry Core's structured 4xx code and message, sanitized."""

from typing import Any

import anyio
import httpx
import pytest

from solwyn_cli import auth
from solwyn_cli.errors import REDACTED
from test_mcp_conformance import VALID_KEY, _call_tool, _install_http

CLI_TOKEN = "solwyn_cli_" + "c3" * 32
DEVICE_CODE = "solwyn_dc_" + "d4" * 32
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlLXZhbHVl"
REJECTED = "The Solwyn API rejected the request."


def _remote_error(
    monkeypatch: pytest.MonkeyPatch, status: int, detail: Any
) -> dict[str, Any]:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    _, _, responses = _install_http(
        monkeypatch,
        responses=[httpx.Response(status, json={"detail": detail})],
        credentials=[key],
    )
    result = anyio.run(
        _call_tool, "get_costs", {"time_range": "7d", "group_by": "model"}
    )
    assert result.is_error
    assert responses == []
    return result.structured_content["error"]


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (
            402,
            {
                "code": "quota_exceeded",
                "message": "Monthly request quota reached (5000/5000)",
                "quota_exceeded": True,
                "included_requests": 5000,
                "current_requests": 5000,
                "window_start": "2026-10-01T00:00:00Z",
                "window_end": "2026-11-01T00:00:00Z",
                "cta_path": "/billing",
            },
            {
                "code": "quota_exceeded",
                "message": "Monthly request quota reached (5000/5000)",
                "hint": "Review usage and plan limits in the dashboard: /billing",
                "status_code": 402,
            },
        ),
        (
            402,
            {
                "code": "upgrade_required",
                "feature": "cost_export",
                "current_tier": "free",
                "required_tier": "production",
                "message": "Cost exports require the Production tier or above",
                "allowed_values": None,
                "cta_path": "/billing",
            },
            {
                "code": "upgrade_required",
                "message": "Cost exports require the Production tier or above",
                "hint": "Review upgrade options in the dashboard: /billing",
                "status_code": 402,
            },
        ),
        (
            402,
            {
                "code": "project_limit_reached",
                "message": "Project limit reached for the free tier (1/1)",
                "current_tier": "free",
                "limit": 1,
                "cta_path": "/billing",
            },
            {
                "code": "project_limit_reached",
                "message": "Project limit reached for the free tier (1/1)",
                "hint": "Manage project limits in the dashboard: /billing",
                "status_code": 402,
            },
        ),
        (
            403,
            {
                "code": "approval_required",
                "message": "This change needs approval in the browser",
                "approval_id": "appr-1",
                "approval_url": "https://app.solwyn.ai/cli/approve/appr-1",
            },
            {
                "code": "approval_required",
                "message": "This change needs approval in the browser",
                "hint": (
                    "A human must approve this change in the browser: "
                    "https://app.solwyn.ai/cli/approve/appr-1"
                ),
                "status_code": 403,
            },
        ),
        (
            403,
            {
                "code": "read_only_key",
                "message": "This API key is read-only",
            },
            {
                "code": "read_only_key",
                "message": "This API key is read-only",
                "hint": (
                    "The configured project key is read-only; set SOLWYN_API_KEY to a "
                    "write-scoped key, then retry. Confirm a key's scope with "
                    "solwyn keys show."
                ),
                "status_code": 403,
            },
        ),
        (
            429,
            {
                "code": "approval_limit_reached",
                "message": "Too many pending approvals",
            },
            {
                "code": "approval_limit_reached",
                "message": "Too many pending approvals",
                "hint": (
                    "Approve or deny pending approvals in the browser, or wait for "
                    "them to expire, then retry."
                ),
                "status_code": 429,
            },
        ),
    ],
    ids=[
        "quota_exceeded",
        "upgrade_required",
        "project_limit_reached",
        "approval_required",
        "read_only_key",
        "approval_limit_reached",
    ],
)
def test_known_4xx_families_carry_the_server_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    detail: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    assert _remote_error(monkeypatch, status, detail) == expected


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (
            403,
            {"code": "permission_required", "message": "Only owners can do this"},
            {
                "code": "permission_required",
                "message": "Only owners can do this",
                "hint": "Check account access for this operation, then retry.",
                "status_code": 403,
            },
        ),
        (
            409,
            {"code": "project_key_conflict", "message": "A key with that name exists"},
            {
                "code": "project_key_conflict",
                "message": "A key with that name exists",
                "hint": None,
                "status_code": 409,
            },
        ),
        (
            404,
            "Project not found",
            {
                "code": "api_error",
                "message": REJECTED,
                "hint": None,
                "status_code": 404,
            },
        ),
    ],
    ids=["unlisted_403_code", "unlisted_409_code", "string_detail"],
)
def test_other_4xx_errors_carry_the_server_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    detail: Any,
    expected: dict[str, Any],
) -> None:
    assert _remote_error(monkeypatch, status, detail) == expected


def test_server_message_redacts_credentials_and_drops_undocumented_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _remote_error(
        monkeypatch,
        402,
        {
            "code": "quota_exceeded",
            "message": (
                f"quota for {VALID_KEY} {CLI_TOKEN} {DEVICE_CODE} {JWT}\n\x1b[31mend"
            ),
            "cta_path": "/billing",
            "private": "provider-secret-value",
        },
    )

    assert error["code"] == "quota_exceeded"
    assert error["message"] == (
        f"quota for {REDACTED} {REDACTED} {REDACTED} {REDACTED} [31mend"
    )
    rendered = str(error)
    for secret in (VALID_KEY, CLI_TOKEN, DEVICE_CODE, JWT, "provider-secret-value"):
        assert secret not in rendered


def test_server_message_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    error = _remote_error(monkeypatch, 409, {"code": "conflict", "message": "x" * 5000})

    assert error["code"] == "conflict"
    assert len(error["message"]) <= 500
    assert error["message"].endswith("...")


@pytest.mark.parametrize(
    "code",
    [
        "Quota Exceeded",
        "quota-exceeded",
        "x" * 65,
        VALID_KEY,
        CLI_TOKEN,
        "http_error",
    ],
)
def test_malformed_server_codes_fall_back_to_fixed_codes(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    error = _remote_error(monkeypatch, 409, {"code": code, "message": "safe"})

    assert error["code"] == "api_error"
    assert error["message"] == "safe"
    assert VALID_KEY not in str(error) and CLI_TOKEN not in str(error)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("", "Request failed with status 409."),
        ("   ", REJECTED),
        ("\x00\x01", REJECTED),
    ],
)
def test_blank_server_messages_fall_back_to_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, message: str, expected: str
) -> None:
    error = _remote_error(monkeypatch, 409, {"code": "conflict", "message": message})

    assert error["message"] == expected


def test_5xx_errors_keep_the_fixed_message(monkeypatch: pytest.MonkeyPatch) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                500, json={"detail": {"code": "boom", "message": "stack trace here"}}
            )
        ],
        credentials=[key],
    )
    result = anyio.run(
        _call_tool, "get_costs", {"time_range": "7d", "group_by": "model"}
    )

    error = result.structured_content["error"]
    assert error["message"] == REJECTED
    assert "stack trace" not in str(result)
