"""Safe typed errors for HTTP and API failures."""

import unicodedata
from typing import Any, TypeAlias
from urllib.parse import urlsplit

import httpx


RawDetail: TypeAlias = str | dict[str, Any]

SERVICE_BUSY_HINT = "Service is busy; retry in a moment."
WRITE_BUSY_HINT = (
    "Service is busy. This write's outcome may be unknown; inspect known state "
    "before retrying in a moment."
)
BUDGET_BUSY_DETAIL = "Budget admission is busy; retry shortly"
BUDGET_BUSY_HINT = "No reservation was made by this request; safe to retry in a moment."
LEGACY_BATCH_BUSY_HINT = (
    "Completed results remain valid; do not replay completed checks. "
    "Reservation state within the failed chunk is unknown; later chunks were not submitted."
)
INGEST_BUSY_HINT = (
    "Nothing recorded by this batch; safe to retry in a moment with the same call_ids."
)
INGEST_UNKNOWN_HINT = (
    "Recording outcome may be unknown; preserve call identity and retry in a moment "
    "with the same call_ids and attempt indices."
)
_INGEST_UNACCEPTED_DETAILS = frozenset(
    {
        BUDGET_BUSY_DETAIL,
        "Account tag registry is busy; retry the batch.",
        "Accounting maintenance is in progress; retry the batch.",
    }
)
MEMBERSHIP_RECOVERY_HINT = "Run solwyn account recover to recover a personal account."


class ApiError(RuntimeError):
    """A structured request failure whose repr does not expose raw detail."""

    def __init__(
        self,
        *,
        status_code: int | None,
        code: str,
        message: str,
        hint: str | None,
        detail: RawDetail | None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail
        self.retry_after = retry_after


def _safe_cta_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.username or parsed.password:
        return None
    return value


def _upgrade_hint(cta_path: str) -> str:
    return f"Review upgrade options in the dashboard: {cta_path}"


def _quota_hint(cta_path: str) -> str:
    return f"Review usage and plan limits in the dashboard: {cta_path}"


def _project_limit_hint(cta_path: str) -> str:
    return f"Manage project limits in the dashboard: {cta_path}"


_ENTITLEMENT_HINTS = {
    "upgrade_required": _upgrade_hint,
    "quota_exceeded": _quota_hint,
    "project_limit_reached": _project_limit_hint,
}
_CROSS_PROJECT_KEY_DETAILS = frozenset(
    {
        "Cannot access agent runs for a different project",
        "Cannot access forecast for a different project",
        "Cannot access a different project",
    }
)


def _response_detail(response: httpx.Response) -> RawDetail | None:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, (str, dict)):
        return detail
    return None


def error_from_response(
    response: httpx.Response,
    *,
    code_override: str | None = None,
) -> ApiError:
    """Turn an unsuccessful response into a safe structured error."""
    detail = _response_detail(response)
    fallback_message = f"Request failed with status {response.status_code}."
    code = code_override or "http_error"
    message = detail if isinstance(detail, str) else fallback_message
    hint = None

    if isinstance(detail, dict):
        server_code = detail.get("code")
        server_message = detail.get("message")
        if code_override is None and isinstance(server_code, str) and server_code:
            code = server_code
        if isinstance(server_message, str) and server_message:
            message = server_message

        hint_builder = (
            _ENTITLEMENT_HINTS.get(code) if response.status_code == 402 else None
        )
        cta_path = _safe_cta_path(detail.get("cta_path"))
        if hint_builder is not None and cta_path is not None:
            hint = hint_builder(cta_path)

        allowed_values = detail.get("allowed_values")
        if (
            response.status_code == 402
            and isinstance(allowed_values, list)
            and all(isinstance(value, str) for value in allowed_values)
        ):
            allowed_hint = "Allowed values: " + ", ".join(allowed_values)
            hint = f"{allowed_hint}. {hint}" if hint else allowed_hint

    if response.status_code == 403:
        role_codes = {
            "permission_required",
            "owner_required",
            "admin_required",
            "role_required",
        }
        if code == "read_only_key":
            hint = "Rotate to a full-scope key with solwyn keys rotate --scope full."
        elif detail == "This endpoint requires JWT authentication":
            hint = "Run solwyn login to authenticate with your user account."
        elif isinstance(detail, str) and detail in _CROSS_PROJECT_KEY_DETAILS:
            hint = (
                "Select the project associated with this key, or configure a key "
                "for the selected project."
            )
        elif code in role_codes or isinstance(detail, str) and detail in role_codes:
            hint = (
                "Your role cannot perform this action; use solwyn whoami to inspect it."
            )

    if response.status_code == 401 and (
        code == "account_membership_revoked" or detail == "account_membership_revoked"
    ):
        code = "account_membership_revoked"
        hint = MEMBERSHIP_RECOVERY_HINT

    if response.status_code == 503:
        if code == "http_error":
            code = "service_busy"
        hint = SERVICE_BUSY_HINT

    return ApiError(
        status_code=response.status_code,
        code=code,
        message=message,
        hint=hint,
        detail=detail,
        retry_after=response.headers.get("Retry-After"),
    )


def is_budget_admission_busy(error: BaseException, *, path: str) -> bool:
    """Prove no reservation only on single checks and the durable journal.

    The legacy batch loop can commit earlier reservations before Core's global
    handler returns the same detail as the pre-auth capacity middleware.
    """
    return (
        urlsplit(path).path.strip("/")
        in {"api/v1/budgets/check", "api/v1/budgets/check/batches"}
        and isinstance(error, ApiError)
        and error.status_code == 503
        and error.detail == BUDGET_BUSY_DETAIL
    )


def is_ingest_unaccepted(error: BaseException) -> bool:
    """Recognize Core's three rolled-back ingest capacity responses.

    Exact fixed details plus Retry-After: 1 are the current runtime contract.
    Generic/proxy 503s and missing headers do not prove absence. Eligibility
    for an identical-call-ID replay is a separate client policy.
    """
    return (
        isinstance(error, ApiError)
        and error.status_code == 503
        and error.retry_after == "1"
        and isinstance(error.detail, str)
        and error.detail in _INGEST_UNACCEPTED_DETAILS
    )
