"""Safe typed errors for HTTP and API failures."""

import unicodedata
from typing import Any, TypeAlias
from urllib.parse import urlsplit

import httpx


RawDetail: TypeAlias = str | dict[str, Any]


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
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail


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

    return ApiError(
        status_code=response.status_code,
        code=code,
        message=message,
        hint=hint,
        detail=detail,
    )
