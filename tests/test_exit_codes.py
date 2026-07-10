"""Frozen CLI process exit-code contract tests."""

from enum import IntEnum

import pytest

from solwyn_cli.auth import AuthError
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, ExitCode, exit_code_for_error


def _api_error(status_code: int, code: str = "http_error") -> ApiError:
    return ApiError(
        status_code=status_code,
        code=code,
        message="Safe message.",
        hint=None,
        detail=None,
    )


def test_exit_code_table_is_frozen_and_complete() -> None:
    assert issubclass(ExitCode, IntEnum)
    assert {member.name: member.value for member in ExitCode} == {
        "SUCCESS": 0,
        "GENERIC_ERROR": 1,
        "USAGE_ERROR": 2,
        "AUTH_ERROR": 3,
        "BUDGET_DENIED": 4,
        "NOT_FOUND": 5,
    }


@pytest.mark.parametrize(
    ("error", "semantic", "expected"),
    [
        (None, None, ExitCode.SUCCESS),
        (RuntimeError("safe"), None, ExitCode.GENERIC_ERROR),
        (RuntimeError("safe"), ErrorSemantic.USAGE, ExitCode.USAGE_ERROR),
        (AuthError("Authentication required."), None, ExitCode.AUTH_ERROR),
        (_api_error(401), None, ExitCode.AUTH_ERROR),
        (_api_error(403), None, ExitCode.AUTH_ERROR),
        (
            _api_error(409, "budget_denied"),
            ErrorSemantic.BUDGET_DENIED,
            ExitCode.BUDGET_DENIED,
        ),
        (_api_error(404), None, ExitCode.NOT_FOUND),
        (_api_error(500), None, ExitCode.GENERIC_ERROR),
    ],
)
def test_typed_errors_map_to_the_frozen_exit_contract(
    error: BaseException | None,
    semantic: ErrorSemantic | None,
    expected: ExitCode,
) -> None:
    assert exit_code_for_error(error, semantic=semantic) is expected


@pytest.mark.parametrize("code", ["upgrade_required", "quota_exceeded"])
def test_entitlement_402_never_infers_budget_denial(code: str) -> None:
    assert exit_code_for_error(_api_error(402, code)) is ExitCode.GENERIC_ERROR
