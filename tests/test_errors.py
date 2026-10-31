"""Structured HTTP error parsing and entitlement hint tests."""

from typing import Any

import httpx
import pytest

from solwyn_cli.errors import ApiError, approval_url_from, error_from_response


def _response(status_code: int, payload: Any) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://api.solwyn.ai/api/v1/costs"),
    )


def test_string_detail_is_preserved_and_used_as_the_message() -> None:
    response = _response(400, {"detail": "Choose a valid date range."})

    error = error_from_response(response)

    assert error.status_code == 400
    assert error.code == "http_error"
    assert error.message == "Choose a valid date range."
    assert error.detail == "Choose a valid date range."
    assert error.hint is None


@pytest.mark.parametrize(
    "code",
    ["upgrade_required", "quota_exceeded", "project_limit_reached"],
)
def test_known_402_codes_preserve_raw_detail_and_build_safe_cta_hint(
    code: str,
) -> None:
    detail = {
        "code": code,
        "message": "Your plan does not include this operation.",
        "cta_path": "/billing?source=cli",
        "future": {"nested": ["value"]},
    }

    error = error_from_response(_response(402, {"detail": detail}))

    assert error.status_code == 402
    assert error.code == code
    assert error.message == "Your plan does not include this operation."
    assert error.detail == detail
    assert error.detail["future"] == {"nested": ["value"]}
    assert error.hint is not None
    assert "/billing?source=cli" in error.hint


@pytest.mark.parametrize(
    "cta_path",
    [
        "https://attacker.example/billing",
        "//user:password@attacker.example/billing",
        "/billing\nspoofed",
        "/billing\\attacker.example",
        "billing",
    ],
)
def test_hostile_or_non_relative_cta_paths_are_not_rendered_as_hints(
    cta_path: str,
) -> None:
    detail = {
        "code": "upgrade_required",
        "message": "Upgrade required.",
        "cta_path": cta_path,
    }

    error = error_from_response(_response(402, {"detail": detail}))

    assert error.hint is None
    assert error.detail == detail


def test_unknown_machine_code_uses_server_message_without_a_hint() -> None:
    detail = {
        "code": "future_entitlement_code",
        "message": "A future server message.",
        "cta_path": "/billing",
        "extra": 42,
    }

    error = error_from_response(_response(402, {"detail": detail}))

    assert error.code == "future_entitlement_code"
    assert error.message == "A future server message."
    assert error.hint is None
    assert error.detail == detail


@pytest.mark.parametrize(
    "detail",
    [
        {"code": 42, "message": None, "cta_path": ["/billing"]},
        {"future_only": True},
    ],
)
def test_forward_compatible_dict_details_tolerate_missing_or_non_string_fields(
    detail: dict[str, object],
) -> None:
    error = error_from_response(_response(409, {"detail": detail}))

    assert error.code == "http_error"
    assert error.message == "Request failed with status 409."
    assert error.hint is None
    assert error.detail == detail


def test_malformed_json_response_becomes_safe_typed_error() -> None:
    response = httpx.Response(
        502,
        content=b"upstream response containing secret-body-value",
        request=httpx.Request("GET", "https://api.solwyn.ai/api/v1/costs"),
    )

    error = error_from_response(response)

    assert error.status_code == 502
    assert error.code == "http_error"
    assert error.message == "Request failed with status 502."
    assert error.detail is None
    assert "secret-body-value" not in str(error)
    assert "secret-body-value" not in repr(error)


def test_error_repr_never_includes_preserved_raw_detail() -> None:
    detail = {
        "code": "unknown",
        "message": "Safe message.",
        "nested": {"provider_key": "provider-secret"},
    }

    error = ApiError(
        status_code=400,
        code="unknown",
        message="Safe message.",
        hint=None,
        detail=detail,
    )

    assert error.detail is detail
    assert "provider-secret" not in str(error)
    assert "provider-secret" not in repr(error)


@pytest.mark.parametrize(
    "detail, fragment",
    [
        ({"code": "read_only_key", "message": "Read-only key"}, "full-scope"),
        ("This endpoint requires JWT authentication", "solwyn login"),
        ("owner_required", "whoami"),
        ({"code": "permission_required"}, "whoami"),
        (
            "Cannot access agent runs for a different project",
            "project associated with this key",
        ),
        (
            "Cannot access forecast for a different project",
            "project associated with this key",
        ),
        (
            "Cannot access a different project",
            "project associated with this key",
        ),
    ],
)
def test_403_shapes_have_actionable_hints(detail, fragment):
    error = error_from_response(_response(403, {"detail": detail}))
    assert error.hint is not None
    assert fragment in error.hint


def test_402_allowed_values_are_preserved_in_hint():
    error = error_from_response(
        _response(
            402,
            {
                "detail": {
                    "code": "upgrade_required",
                    "message": "Range unavailable",
                    "allowed_values": ["7d", "30d"],
                    "cta_path": "/billing",
                }
            },
        )
    )
    assert error.message == "Range unavailable"
    assert "7d, 30d" in error.hint
    assert "/billing" in error.hint


def test_seat_limit_422_is_ready_for_deferred_team_commands() -> None:
    detail = {
        "code": "seat_limit_exceeded",
        "message": "Seat limit exceeded for the account tier.",
        "current_seats": 5,
        "target_seat_limit": 5,
    }

    error = error_from_response(_response(422, {"detail": detail}))

    assert error.status_code == 422
    assert error.code == "seat_limit_exceeded"
    assert error.message == "Seat limit exceeded for the account tier."
    assert error.detail == detail


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://[::1]:5173/approve/abc", "http://[::1]:5173/approve/abc"),
        ("https://app.solwyn.ai./approve/abc", "https://app.solwyn.ai/approve/abc"),
        (
            "https://bücher.example/approve/abc",
            "https://xn--bcher-kva.example/approve/abc",
        ),
        ("https://APP.Solwyn.AI/approve/ABC-1", "https://app.solwyn.ai/approve/ABC-1"),
        ("http://127.0.0.1:5173/approve/abc", "http://127.0.0.1:5173/approve/abc"),
        ("https://[2001:db8::1]/approve/abc", "https://[2001:db8::1]/approve/abc"),
        ("http://[2001:db8::1]/approve/abc", None),
        ("https://a_b.example/approve/abc", None),
        ("https://faß.de/approve/abc", "https://xn--fa-hia.de/approve/abc"),
        (
            "https://\u03c4\u03b5\u03c3\u03c4\u03bf\u03c2.example/approve/abc",
            "https://xn--qxaujcdc.example/approve/abc",
        ),
        ("https://[fe80::1%eth0]/approve/abc", None),
        ("https://[fe80::1%eth0 IGNORE_PREVIOUS_INSTRUCTIONS]/approve/abc", None),
    ],
)
def test_approval_url_accepts_every_core_origin_shape(
    url: str, expected: str | None
) -> None:
    assert approval_url_from({"approval_url": url}) == expected
