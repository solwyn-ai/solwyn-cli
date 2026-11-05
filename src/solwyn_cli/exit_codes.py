"""Stable process exit codes for the Solwyn CLI machine contract."""

from enum import IntEnum, StrEnum

from solwyn_cli.auth import AuthError
from solwyn_cli.errors import ApiError


class ExitCode(IntEnum):
    """Permanently frozen exit meanings across all CLI major versions."""

    SUCCESS = 0
    GENERIC_ERROR = 1
    USAGE_ERROR = 2
    AUTH_ERROR = 3
    BUDGET_DENIED = 4
    NOT_FOUND = 5
    APPROVAL_REQUIRED = 6


class ErrorSemantic(StrEnum):
    """Explicit local semantics that cannot be inferred from HTTP status alone."""

    GENERIC = "generic"
    USAGE = "usage"
    AUTH = "auth"
    BUDGET_DENIED = "budget_denied"
    NOT_FOUND = "not_found"
    APPROVAL_REQUIRED = "approval_required"


_SEMANTIC_EXIT_CODES = {
    ErrorSemantic.GENERIC: ExitCode.GENERIC_ERROR,
    ErrorSemantic.USAGE: ExitCode.USAGE_ERROR,
    ErrorSemantic.AUTH: ExitCode.AUTH_ERROR,
    ErrorSemantic.BUDGET_DENIED: ExitCode.BUDGET_DENIED,
    ErrorSemantic.NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorSemantic.APPROVAL_REQUIRED: ExitCode.APPROVAL_REQUIRED,
}

# The change was not applied because a browser approval is required, pending,
# denied, expired or no longer matches the request.
APPROVAL_ERROR_CODES = frozenset(
    {
        "approval_required",
        "approval_pending",
        "approval_denied",
        "approval_expired",
        "approval_invalid",
        "approval_limit_reached",
        "login_pending",
    }
)


# A project selector that names no project, several, or conflicting ones: the
# command's input is wrong, whichever side (Core or the local resolver) found it.
PROJECT_SELECTION_ERROR_CODES = frozenset(
    {
        "project_selector_required",
        "project_not_found",
        "project_selector_conflict",
        "project_ambiguous",
        "project_mismatch",
        "api_key_account_mismatch",
        "project_required",
    }
)


def exit_code_for_error(
    error: BaseException | None,
    *,
    semantic: ErrorSemantic | None = None,
) -> ExitCode:
    """Map typed failures without interfering with Click's native usage handling."""
    if getattr(error, "code", None) in APPROVAL_ERROR_CODES:
        return ExitCode.APPROVAL_REQUIRED
    if (
        semantic is None
        and getattr(error, "code", None) in PROJECT_SELECTION_ERROR_CODES
    ):
        return ExitCode.USAGE_ERROR
    if semantic is not None:
        return _SEMANTIC_EXIT_CODES[semantic]
    if error is None:
        return ExitCode.SUCCESS
    if isinstance(error, AuthError):
        return ExitCode.AUTH_ERROR
    if isinstance(error, ApiError):
        if error.status_code in {401, 403}:
            return ExitCode.AUTH_ERROR
        if error.status_code == 404:
            return ExitCode.NOT_FOUND
    return ExitCode.GENERIC_ERROR
