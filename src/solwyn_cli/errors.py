"""Safe typed errors for HTTP and API failures."""

import ipaddress
import re
import unicodedata
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

import httpx
import idna
from pydantic import BaseModel, ConfigDict, ValidationError


RawDetail: TypeAlias = str | dict[str, Any]

# Spec section 4: CLI credentials and device codes never reach any output.
_SECRET_TOKEN = re.compile(r"\bsolwyn_(?:cli|dc)_[0-9a-f]{64}\b")
REDACTED = "[REDACTED]"


def redact_secrets(value: Any) -> Any:
    """Replace CLI tokens and device codes anywhere inside a JSON-like value."""
    if isinstance(value, str):
        return _SECRET_TOKEN.sub(REDACTED, value)
    if isinstance(value, dict):
        return {
            redact_secrets(key): redact_secrets(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


SERVICE_BUSY_HINT = "Service is busy; retry in a moment."
WRITE_BUSY_HINT = (
    "Service is busy. This write's outcome may be unknown; inspect known state "
    "before retrying in a moment."
)
BUDGET_BUSY_DETAIL = "Budget admission is busy; retry shortly"
BUDGET_BUSY_HINT = "No reservation was made by this request; safe to retry in a moment."
LEGACY_BATCH_BUSY_HINT = (
    "Reservation state within the failed chunk is unknown; do not blindly replay "
    "the failed chunk. Later chunks were not submitted."
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
NO_ACCOUNT_URL = "https://app.solwyn.ai/no-account"
MEMBERSHIP_RECOVERY_HINT = (
    f"Create a personal account at {NO_ACCOUNT_URL}, then run solwyn login."
)
CLI_CREDENTIAL_HINTS = {
    "cli_credential_invalid": "Your Solwyn login is not valid. Run solwyn login.",
    "cli_credential_expired": "Your Solwyn login expired. Run solwyn login.",
    "cli_credential_revoked": "Your Solwyn login was revoked. Run solwyn login.",
}
APPROVAL_USED_HINT = (
    "The approval was used. Run the command again to request a new one."
)
APPROVAL_MAYBE_USED_HINT = "This approval may have been used; if a retry reports it consumed, request a new one."
APPROVAL_INVALID_MESSAGES = {
    "not_found": "The approval was not found for this login.",
    "pending": "The approval is still pending. Approve it in the browser, then retry with --approval.",
    "denied": "The approval was denied in the browser.",
    "expired": "The approval expired.",
    "consumed": "The approval was already used.",
    "mismatch": "The request changed since it was approved.",
}
APPROVAL_INVALID_HINTS = {
    "pending": None,
    "mismatch": "Run the command again to request a new approval.",
}
APPROVAL_RETRY_HINT = "Run the command again to request a new approval."


BILLING_WRITE_UNKNOWN_HINT = (
    "Billing write outcome may be unknown; inspect solwyn plan and the hosted "
    "billing state before retrying. Do not blindly replay this action."
)
BILLING_HINTS = {
    "billing_cancel_pending": "Inspect solwyn plan; use solwyn plan resume to clear cancellation before changing plans.",
    "billing_plan_change_scheduled": "Inspect solwyn plan; use solwyn plan change --discard before cancelling.",
    "billing_plan_payment_failed": "Inspect solwyn plan and invoices; use solwyn plan portal to resolve payment before a deliberate retry.",
    "billing_subscription_active": "Use solwyn plan change --tier T --interval month|year for an existing subscription.",
    "billing_subscription_past_due": "Inspect solwyn plan; use solwyn plan portal to resolve the outstanding invoice.",
    "billing_subscription_unusable": "Inspect solwyn plan and use solwyn plan portal to resolve the subscription state.",
    "billing_plan_no_change": "Inspect solwyn plan and solwyn plan list; choose a different tier or interval.",
    "billing_cancel_unavailable": BILLING_WRITE_UNKNOWN_HINT,
    "billing_plan_change_unavailable": BILLING_WRITE_UNKNOWN_HINT,
    "billing_checkout_unavailable": "Checkout session creation outcome may be unknown; inspect solwyn plan and hosted checkout before creating another session.",
    "billing_portal_unavailable": "Billing portal session creation outcome may be unknown; inspect known hosted sessions before creating another portal session.",
    "billing_preview_unavailable": "The preview is read-only and has no billing effect; retry the preview in a moment.",
    "billing_subscription_not_found": "Inspect solwyn plan; use solwyn plan upgrade --tier T --interval month|year to start paid checkout.",
    "billing_customer_not_found": "Inspect solwyn plan; use solwyn plan upgrade --tier T --interval month|year to start paid checkout.",
    "billing_plan_change_validation": "Inspect solwyn plan and solwyn plan list, then choose an eligible plan.",
    "seat_limit_exceeded": "Inspect solwyn whoami and solwyn plan list; choose a plan that accommodates the account seats.",
}


class _BillingErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class BillingConflictDetail(_BillingErrorDetail):
    code: Literal[
        "billing_subscription_past_due",
        "billing_subscription_unusable",
        "billing_subscription_active",
        "billing_plan_no_change",
        "billing_cancel_pending",
        "billing_plan_change_scheduled",
        "billing_plan_payment_failed",
    ]
    message: str
    status: str | None = None


class BillingUnavailableDetail(_BillingErrorDetail):
    code: Literal[
        "billing_checkout_unavailable",
        "billing_portal_unavailable",
        "billing_preview_unavailable",
        "billing_plan_change_unavailable",
        "billing_cancel_unavailable",
        "billing_webhook_unavailable",
    ]
    message: str


class BillingNotFoundDetail(_BillingErrorDetail):
    code: Literal[
        "billing_subscription_not_found",
        "billing_customer_not_found",
        "webhook_event_not_found",
    ]
    message: str


class BillingValidationDetail(_BillingErrorDetail):
    code: Literal["billing_plan_change_validation", "seat_limit_exceeded"]
    message: str
    current_seats: int | None = None
    target_seat_limit: int | None = None


_BILLING_ERROR_MODELS = {
    409: BillingConflictDetail,
    503: BillingUnavailableDetail,
    404: BillingNotFoundDetail,
    422: BillingValidationDetail,
}


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
        ingest_outcome_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail
        self.retry_after = retry_after
        # Whole-call evidence supplied by the client, never a public error field.
        self.ingest_outcome_uncertain = ingest_outcome_uncertain
        # What is known about an attached approval after this failure, if any.
        self.approval_note: str | None = None


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

    if response.status_code == 401 and code in CLI_CREDENTIAL_HINTS:
        hint = CLI_CREDENTIAL_HINTS[code]

    if isinstance(detail, dict):
        if response.status_code == 403 and code == "approval_required":
            approval_url = approval_url_from(detail)
            hint = (
                f"Approve the change in the browser: {approval_url}"
                if approval_url is not None
                else "Approve the change in the browser."
            )
        elif response.status_code == 403 and code == "approval_invalid":
            reason = detail.get("reason")
            if isinstance(reason, str) and reason in APPROVAL_INVALID_MESSAGES:
                message = APPROVAL_INVALID_MESSAGES[reason]
                hint = APPROVAL_INVALID_HINTS.get(reason, APPROVAL_RETRY_HINT)
        elif response.status_code == 403 and code == "session_required":
            hint = "This action is only available in the dashboard."
        elif response.status_code == 429 and code == "approval_limit_reached":
            hint = (
                "You have 5 pending approvals. Approve or deny them in the browser, "
                "or wait for them to expire, then retry."
            )
        elif response.status_code == 410 and code == "cli_password_login_retired":
            server_hint = detail.get("hint")
            hint = (
                server_hint
                if isinstance(server_hint, str) and server_hint
                else "Upgrade solwyn-cli and run solwyn login."
            )

    if response.status_code == 503:
        if code == "http_error":
            code = "service_busy"
        hint = SERVICE_BUSY_HINT

    if code in BILLING_HINTS or code.startswith("billing_"):
        model = _BILLING_ERROR_MODELS.get(response.status_code)
        try:
            if model is None:
                raise ValueError("Unexpected billing error status.")
            parsed = model.model_validate(detail)
        except (ValidationError, ValueError, TypeError):
            code = "invalid_billing_error_response"
            message = "The billing error response was invalid."
            detail = None
            hint = "Inspect solwyn plan before retrying a billing action."
        else:
            detail = parsed.model_dump(exclude_unset=True)
            hint = BILLING_HINTS.get(code)

    return ApiError(
        status_code=response.status_code,
        code=code,
        message=message,
        hint=hint,
        detail=detail,
        retry_after=response.headers.get("Retry-After"),
    )


def safe_absolute_url(value: object) -> str | None:
    """Return an absolute http(s) URL without credentials or control characters."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return value


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _approval_authority(host: str, port: int | None) -> tuple[str, bool] | None:
    """Return a rebuilt ``host[:port]`` and whether it is loopback, or None.

    IP literals are parsed as addresses (IPv6 comes back bracketed); names are
    IDNA-encoded to ASCII and every label checked, so only a hostname or an
    address can come out, never other server text.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        # A zone id ("fe80::1%eth0 ...") is free text that survives .compressed.
        if getattr(address, "scope_id", None) is not None:
            return None
        rebuilt = (
            f"[{address.compressed}]" if address.version == 6 else address.compressed
        )
        loopback = address.is_loopback
    else:
        name = host[:-1] if host.endswith(".") else host
        if "%" in name:
            return None
        try:
            # UTS 46 non-transitional, as browsers resolve it: faß.de is xn--fa-hia.de.
            ascii_name = idna.encode(name, uts46=True, transitional=False).decode(
                "ascii"
            )
        except (idna.IDNAError, UnicodeError):
            return None
        labels = ascii_name.split(".")
        if len(ascii_name) > 253 or not all(
            _DNS_LABEL.fullmatch(label) for label in labels
        ):
            return None
        rebuilt = ascii_name
        loopback = ascii_name == "localhost"
    return (rebuilt if port is None else f"{rebuilt}:{port}"), loopback


def approval_url_from(detail: object) -> str | None:
    """Rebuild Core's approval link from strictly validated parts, if present.

    Core sends ``<app origin>/approve/<approval id>``. The link is shown to a
    human and handed to agents over MCP, so the server's string is never
    passed through: anything with a query, fragment, credentials, extra path or
    characters outside a hostname and an id is dropped, and the returned URL is
    built from the validated scheme, host, port and id only.
    """
    if not isinstance(detail, dict):
        return None
    value = safe_absolute_url(detail.get("approval_url"))
    if value is None:
        return None
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.query or parsed.fragment or not parsed.hostname:
        return None
    checked = _approval_authority(parsed.hostname, port)
    if checked is None:
        return None
    authority, loopback = checked
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        return None
    segments = parsed.path.split("/")
    if len(segments) != 3 or segments[0] or segments[1] != "approve":
        return None
    approval_id = segments[2]
    if not _APPROVAL_ID.fullmatch(approval_id):
        return None
    expected_id = detail.get("approval_id")
    if expected_id is not None and expected_id != approval_id:
        return None
    return f"{parsed.scheme}://{authority}/approve/{approval_id}"


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
    """Prove a logical ingest was unaccepted using all its attempt evidence.

    Exact fixed details plus Retry-After: 1 are the current runtime contract.
    Generic/proxy 503s and missing headers do not prove absence, even when a
    later retry is rejected by Core. Eligibility for an identical-call-ID
    replay is a separate client policy.
    """
    return (
        isinstance(error, ApiError)
        and not error.ingest_outcome_uncertain
        and error.status_code == 503
        and error.retry_after == "1"
        and isinstance(error.detail, str)
        and error.detail in _INGEST_UNACCEPTED_DETAILS
    )
