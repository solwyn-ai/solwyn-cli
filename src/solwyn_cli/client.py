"""Synchronous authenticated HTTP client with bounded retries."""

import math
import time
import re
from collections.abc import Callable, Mapping, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from solwyn_cli import __version__
from solwyn_cli.approvals import (
    APPROVAL_HEADER,
    APPROVAL_POLL_SECONDS,
    ApprovalSettings,
    current_settings,
    may_need_approval,
    retry_argv,
    retry_command,
)
from solwyn_cli.auth import (
    AuthError,
    ResolvedCredential,
    resolve_credential,
)
from solwyn_cli.errors import (
    APPROVAL_MAYBE_USED_HINT,
    APPROVAL_USED_HINT,
    BILLING_HINTS,
    BILLING_WRITE_UNKNOWN_HINT,
    ApiError,
    BUDGET_BUSY_HINT,
    INGEST_BUSY_HINT,
    INGEST_UNKNOWN_HINT,
    LEGACY_BATCH_BUSY_HINT,
    SERVICE_BUSY_HINT,
    WRITE_BUSY_HINT,
    approval_url_from,
    error_from_response,
    redact_secrets,
    is_budget_admission_busy,
    is_ingest_unaccepted,
)


CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
WRITE_TIMEOUT_SECONDS = 30.0
POOL_TIMEOUT_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 10.0

_UNSET = object()
# Server 401 codes that carry their own safe message and hint.
_EXPLAINED_UNAUTHORIZED_CODES = frozenset(
    {
        "account_membership_revoked",
        "cli_credential_invalid",
        "cli_credential_expired",
        "cli_credential_revoked",
    }
)
_APPROVAL_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_EFFECT_TEXT = {
    "raises_cap": "raises the cap",
    "lowers_cap": "lowers the cap",
    "weakens_enforcement": "weakens enforcement",
    "strengthens_enforcement": "strengthens enforcement",
    "changes_period": "changes the period",
    "changes_rules": "changes rules",
}


class ApprovalChange(BaseModel):
    """One described field change on an approval page."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    field: str
    label: str
    before: str | None = None
    after: str | None = None
    effect: str | None = None


class ApprovalView(BaseModel):
    """The approval fields the CLI shows and decides on."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str
    status: Literal["pending", "approved", "denied", "consumed", "expired"]
    action: str
    title: str
    reasons: list[str]
    changes: list[ApprovalChange]
    created_at: AwareDatetime
    expires_at: AwareDatetime


def _terminal_safe(value: object) -> str:
    return "".join(
        "\N{REPLACEMENT CHARACTER}"
        if ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        else character
        for character in redact_secrets(str(value))
    )


# Failures that can come before or after the gate consumes an approval. Core
# consumes in the auth dependency, which can run before a route's own limiter.
_UNCERTAIN_APPROVAL_STATUSES = frozenset({None, 429, 503})


def _mark_approval_spent(error: ApiError) -> None:
    """Say what is known about an attached approval after a failed change."""
    if (
        error.code.startswith("approval_")
        or error.code == "session_required"
        or error.status_code == 401
    ):
        # Authentication and the gate refuse before anything is consumed.
        return
    error.approval_note = (
        APPROVAL_MAYBE_USED_HINT
        if error.status_code in _UNCERTAIN_APPROVAL_STATUSES
        else APPROVAL_USED_HINT
    )
    error.hint = (
        f"{error.hint} {error.approval_note}" if error.hint else error.approval_note
    )


def _pending_error(
    approval_id: str,
    *,
    approval_url: str | None,
    expires_at: str,
    notes: list[str],
) -> ApiError:
    """Name the exact command that re-sends this change once it is approved."""
    argv = retry_argv(approval_id)
    if argv is None:
        error = approval_error(
            "approval_pending",
            f"Still waiting for approval {approval_id}. After approving, run the "
            f"same command again with --approval {approval_id}.",
            hint=None,
            approval_id=approval_id,
            approval_url=approval_url,
            expires_at=expires_at,
        )
    else:
        command = retry_command(argv)
        error = approval_error(
            "approval_pending",
            f"Still waiting for approval {approval_id}.",
            hint=" ".join([f"After approving, run: {command}", *notes]),
            approval_id=approval_id,
            approval_url=approval_url,
            expires_at=expires_at,
        )
        if isinstance(error.detail, dict):
            error.detail["retry_command"] = command
            error.detail["retry_argv"] = argv
    return error


def approval_path(approval_id: str) -> str:
    """Return the approval read path for a validated approval ID."""
    return f"/api/v1/cli/approvals/{quote(approval_id, safe='')}"


def approval_error(
    code: str,
    message: str,
    *,
    hint: str | None,
    approval_id: str,
    approval_url: str | None = None,
    expires_at: str | None = None,
) -> ApiError:
    """Build a local approval outcome error with its stable detail fields."""
    detail: dict[str, Any] = {"approval_id": approval_id}
    if approval_url is not None:
        detail["approval_url"] = approval_url
    if expires_at is not None:
        detail["expires_at"] = expires_at
    return ApiError(
        status_code=403,
        code=code,
        message=message,
        hint=hint,
        detail=detail,
    )


@dataclass
class _RequestEvidence:
    """Shared retry budget and ingest evidence across capacity/auth attempts."""

    capacity_retry_used: bool = False
    ingest_outcome_uncertain: bool = False


def _contains_dot_segment(path: str) -> bool:
    decoded_path = path
    while True:
        normalized_path = decoded_path.replace("\\", "/")
        if any(segment in {".", ".."} for segment in normalized_path.split("/")):
            return True
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            return False
        decoded_path = next_path


def _retry_after_seconds(value: str | None, now: datetime) -> float:
    if value is None:
        return 0.0
    try:
        integer_delay = int(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        delay = (retry_at - now).total_seconds()
        return min(MAX_RETRY_AFTER_SECONDS, max(0.0, delay))
    return float(min(int(MAX_RETRY_AFTER_SECONDS), max(0, integer_delay)))


class SolwynClient:
    """A closeable synchronous client for authenticated Solwyn API requests."""

    def __init__(
        self,
        *,
        api_url: str,
        credential: ResolvedCredential | None | object = _UNSET,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], object] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_api_url = f"{api_url.rstrip('/')}/"
        self._api_url = normalized_api_url.rstrip("/")
        self._credential = credential
        self._sleeper = sleeper
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = httpx.Client(
            base_url=normalized_api_url,
            headers={"User-Agent": f"solwyn-cli/{__version__}"},
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=WRITE_TIMEOUT_SECONDS,
                pool=POOL_TIMEOUT_SECONDS,
            ),
            transport=transport,
        )

    @property
    def is_closed(self) -> bool:
        """Report whether the underlying connection pool is closed."""
        return self._client.is_closed

    @property
    def current_credential(self) -> ResolvedCredential:
        """Return the credential this client sends."""
        return self._resolve_credential()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "SolwynClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _resolve_credential(self) -> ResolvedCredential:
        credential = (
            resolve_credential()
            if self._credential is _UNSET
            else cast(ResolvedCredential | None, self._credential)
        )
        if credential is None:
            raise AuthError("Authentication required. Configure credentials and retry.")
        return credential

    def _prepare_request(
        self,
        path: str,
        kwargs: dict[str, Any],
    ) -> tuple[str, httpx.Headers]:
        """Validate a relative path and fixed-timeout request options."""
        parsed_path = urlsplit(path)
        if parsed_path.scheme or parsed_path.netloc:
            raise ApiError(
                status_code=None,
                code="invalid_request",
                message="API request paths must be relative.",
                hint=None,
                detail=None,
            )
        if _contains_dot_segment(parsed_path.path):
            raise ApiError(
                status_code=None,
                code="invalid_request",
                message="API request path is invalid.",
                hint=None,
                detail=None,
            )
        request_extensions = kwargs.get("extensions")
        if "timeout" in kwargs or (
            isinstance(request_extensions, Mapping) and "timeout" in request_extensions
        ):
            raise ApiError(
                status_code=None,
                code="invalid_request",
                message="Request timeouts are fixed by the CLI.",
                hint=None,
                detail=None,
            )
        request_headers = httpx.Headers(kwargs.pop("headers", None))
        request_headers["User-Agent"] = f"solwyn-cli/{__version__}"
        return path.lstrip("/"), request_headers

    def _send_with_capacity_retry(
        self,
        *,
        request: httpx.Request,
        path: str,
        evidence: _RequestEvidence,
        retry_rate_limit: bool = True,
        stream: bool = False,
    ) -> httpx.Response:
        """Share one bounded 429/eligible-503 retry for one prepared request.

        retry_rate_limit controls only 429; eligible 503 retries remain enabled.
        Every attempt sends the same prepared request bytes.
        """
        method = request.method

        def send() -> httpx.Response:
            response = self._client.send(request, stream=stream)
            if stream and not response.is_success:
                try:
                    response.read()
                finally:
                    response.close()
            return response

        response = send()
        while True:
            endpoint = urlsplit(path).path.rstrip("/")
            retryable = False
            if (
                retry_rate_limit
                and response.status_code == 429
                and error_from_response(response).code != "approval_limit_reached"
            ):
                retryable = True
            elif response.status_code == 503:
                safe_busy_retry = "Retry-After" in response.headers and (
                    method.upper() == "GET"
                    or method.upper() == "POST"
                    and endpoint == "api/v1/metadata/ingest"
                )
                if safe_busy_retry:
                    retryable = True
            if not retryable or evidence.capacity_retry_used:
                return response
            # Preserve evidence from the attempt being replayed. Core's auth
            # dependency and 429 middleware/project gate run before ingestion;
            # other retried failures need the exact rollback contract. The final
            # response is classified together with this evidence below.
            if (
                method.upper() == "POST"
                and endpoint == "api/v1/metadata/ingest"
                and response.status_code not in (401, 429)
                and not is_ingest_unaccepted(error_from_response(response))
            ):
                evidence.ingest_outcome_uncertain = True
            evidence.capacity_retry_used = True
            delay = _retry_after_seconds(
                response.headers.get("Retry-After"), self._clock()
            )
            self._sleeper(delay)
            response.close()
            response = send()

    @staticmethod
    def _raise_for_unsuccessful_response(
        response: httpx.Response, *, method: str, path: str, evidence: _RequestEvidence
    ) -> None:
        if not response.is_success:
            error = error_from_response(response)
            if (
                response.status_code == httpx.codes.TOO_MANY_REQUESTS
                and error.code != "approval_limit_reached"
            ):
                error = error_from_response(response, code_override="rate_limited")
            error.ingest_outcome_uncertain = evidence.ingest_outcome_uncertain
            endpoint = urlsplit(path).path.rstrip("/")
            if (
                method.upper() == "POST"
                and endpoint == "api/v1/metadata/ingest"
                and (response.status_code == 503 or error.ingest_outcome_uncertain)
            ):
                error.hint = (
                    INGEST_BUSY_HINT
                    if is_ingest_unaccepted(error)
                    else INGEST_UNKNOWN_HINT
                )
                raise error
            if response.status_code == 503 and method.upper() != "GET":
                if error.code != "auth_capacity_exceeded":
                    error.hint = WRITE_BUSY_HINT
                if method.upper() == "POST":
                    if endpoint == "api/v1/budgets/check/batch":
                        error.hint = LEGACY_BATCH_BUSY_HINT
                    elif is_budget_admission_busy(error, path=endpoint):
                        error.hint = BUDGET_BUSY_HINT
                    else:
                        parts = endpoint.split("/")
                        if (
                            len(parts) == 6
                            and parts[:3] == ["api", "v1", "projects"]
                            and parts[3]
                            and parts[4:] == ["costs", "simulate"]
                        ):
                            # Core's simulation route only reads historical usage.
                            # Keep its service/read hint without enabling POST replay.
                            error.hint = SERVICE_BUSY_HINT
            if (
                response.status_code == 503
                and method.upper() != "GET"
                and endpoint.startswith("api/v1/billing/")
            ):
                error.hint = BILLING_HINTS.get(
                    error.code,
                    SERVICE_BUSY_HINT
                    if endpoint == "api/v1/billing/plan-preview"
                    else BILLING_WRITE_UNKNOWN_HINT,
                )
            raise error

    def request_unauthenticated(
        self,
        method: str,
        path: str,
        *,
        accepted_statuses: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a guarded request without resolving or sending credentials."""
        relative_path, request_headers = self._prepare_request(path, kwargs)
        evidence = _RequestEvidence()
        try:
            response = self._send_with_capacity_retry(
                request=self._client.build_request(
                    method, relative_path, headers=request_headers, **kwargs
                ),
                path=relative_path,
                evidence=evidence,
            )
        except httpx.HTTPError:
            raise ApiError(
                status_code=None,
                code="transport_error",
                message="Could not connect to the Solwyn API.",
                hint=INGEST_UNKNOWN_HINT if evidence.ingest_outcome_uncertain else None,
                detail=None,
                ingest_outcome_uncertain=evidence.ingest_outcome_uncertain,
            ) from None
        if response.status_code not in accepted_statuses:
            self._raise_for_unsuccessful_response(
                response, method=method, path=relative_path, evidence=evidence
            )
        return response

    def request(
        self,
        method: str,
        path: str,
        *,
        retry_rate_limit: bool = True,
        _stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make one logical request with bounded capacity retries.

        retry_rate_limit=False disables 429 retries only; eligible GET/ingest
        503 responses still share the single capacity retry budget. A write
        that needs a browser approval waits for it when the invocation allows,
        then re-sends the exact prepared request once with Solwyn-Approval.
        """
        relative_path, request_headers = self._prepare_request(path, kwargs)
        credential = self._resolve_credential()
        evidence = _RequestEvidence()
        settings = current_settings()
        is_change = may_need_approval(method, relative_path)
        request_headers["Authorization"] = f"Bearer {credential.bearer_value()}"
        attached = (
            settings.take_approval_id() if is_change and settings is not None else None
        )
        if attached is not None:
            request_headers[APPROVAL_HEADER] = attached
        prepared = self._client.build_request(
            method, relative_path, headers=request_headers, **kwargs
        )
        approval_spent = attached is not None

        try:
            response = self._send_with_capacity_retry(
                request=prepared,
                path=relative_path,
                evidence=evidence,
                # Never replay a write that carries an approval: Core may have
                # consumed it before the rejection (its route limiter can run
                # after the gate), and a replay would hide the real error.
                retry_rate_limit=retry_rate_limit and attached is None,
                stream=_stream,
            )
            self._check_authentication(response, evidence)
            if (
                is_change
                and settings is not None
                and attached is None
                and response.status_code == httpx.codes.FORBIDDEN
                and error_from_response(response).code == "approval_required"
            ):
                approval_spent = True
                response = self._approval_handoff(
                    prepared,
                    response,
                    settings,
                    path=relative_path,
                    evidence=evidence,
                    retry_rate_limit=retry_rate_limit,
                    stream=_stream,
                )
        except httpx.HTTPError:
            transport = ApiError(
                status_code=None,
                code="transport_error",
                message="Could not connect to the Solwyn API.",
                hint=(
                    INGEST_UNKNOWN_HINT
                    if evidence.ingest_outcome_uncertain
                    else "The preview is read-only; retry the preview when connectivity returns."
                    if relative_path.rstrip("/") == "api/v1/billing/plan-preview"
                    else BILLING_WRITE_UNKNOWN_HINT
                    if relative_path.startswith("api/v1/billing/")
                    and method.upper() != "GET"
                    else None
                ),
                detail=None,
                ingest_outcome_uncertain=evidence.ingest_outcome_uncertain,
            )
            if approval_spent:
                _mark_approval_spent(transport)
            raise transport from None

        try:
            self._raise_for_unsuccessful_response(
                response, method=method, path=relative_path, evidence=evidence
            )
        except ApiError as error:
            if approval_spent:
                _mark_approval_spent(error)
            raise
        return response

    @staticmethod
    def _check_authentication(
        response: httpx.Response, evidence: _RequestEvidence
    ) -> None:
        """Turn an unexplained 401 into a credential-free auth failure."""
        if response.status_code != httpx.codes.UNAUTHORIZED:
            return
        if error_from_response(response).code in _EXPLAINED_UNAUTHORIZED_CODES:
            return
        error = AuthError("Authentication failed. Check your credentials.")
        error.ingest_outcome_uncertain = evidence.ingest_outcome_uncertain
        raise error

    def read_approval(self, approval_id: str) -> ApprovalView:
        """Read one approval owned by the current login."""
        response = self.request("GET", approval_path(approval_id))
        return ApprovalView.model_validate(response.json())

    def _approval_handoff(
        self,
        prepared: httpx.Request,
        response: httpx.Response,
        settings: ApprovalSettings,
        *,
        path: str,
        evidence: _RequestEvidence,
        retry_rate_limit: bool,
        stream: bool,
    ) -> httpx.Response:
        """Print the approval URL, wait for a decision, then re-send once."""
        required = error_from_response(response)
        detail = required.detail if isinstance(required.detail, dict) else {}
        approval_id = detail.get("approval_id")
        approval_url = approval_url_from(detail)
        if not isinstance(approval_id, str) or not _APPROVAL_ID.fullmatch(approval_id):
            return response
        try:
            view = self.read_approval(approval_id)
        except (ValidationError, ValueError, TypeError):
            raise ApiError(
                status_code=None,
                code="invalid_approval_response",
                message="The approval response was invalid.",
                hint=None,
                detail=None,
            ) from None
        self._print_approval_request(view, approval_url, settings)
        status = self._wait_for_decision(view, settings.wait_seconds)
        expires_at = view.expires_at.isoformat().replace("+00:00", "Z")
        if status == "pending":
            raise _pending_error(
                approval_id,
                approval_url=approval_url,
                expires_at=expires_at,
                notes=settings.notes,
            )
        if status == "denied":
            raise approval_error(
                "approval_denied",
                "The change was denied in the browser. Nothing was sent.",
                hint=None,
                approval_id=approval_id,
            )
        if status != "approved":
            raise approval_error(
                "approval_expired" if status == "expired" else "approval_invalid",
                "The approval expired before it was used."
                if status == "expired"
                else "The approval was already used.",
                hint="Run the command again to request a new approval.",
                approval_id=approval_id,
            )
        settings.write("Approved. Applying the change.\n")
        retry_headers = prepared.headers.copy()
        retry_headers[APPROVAL_HEADER] = approval_id
        retry = httpx.Request(
            prepared.method,
            prepared.url,
            headers=retry_headers,
            content=prepared.read(),
            extensions=prepared.extensions,
        )
        retried = self._send_with_capacity_retry(
            request=retry,
            path=path,
            evidence=evidence,
            retry_rate_limit=False,  # Carries the approval: never replay it.
            stream=stream,
        )
        self._check_authentication(retried, evidence)
        return retried

    def _print_approval_request(
        self,
        view: ApprovalView,
        approval_url: str | None,
        settings: ApprovalSettings,
    ) -> None:
        lines = ["This change needs your approval in the browser:", ""]
        if approval_url is not None:
            lines.extend([f"  {_terminal_safe(approval_url)}", ""])
        lines.append(f"  {_terminal_safe(view.title)}")
        for change in view.changes:
            effect = _EFFECT_TEXT.get(change.effect or "", change.effect or "")
            line = (
                f"  {change.label}: {change.before or 'none'} -> "
                f"{change.after or 'none'}"
            )
            if effect:
                line += f" ({effect.replace('_', ' ')})"
            lines.append(_terminal_safe(line))
        remaining = (view.expires_at - self._clock()).total_seconds()
        minutes = max(1, math.ceil(remaining / 60))
        lines.extend(
            [
                "",
                f"Waiting for approval... (expires in {minutes} "
                f"minute{'s' if minutes != 1 else ''}, Ctrl-C to stop)",
                "",
            ]
        )
        settings.write("\n".join(lines))

    def _wait_for_decision(self, view: ApprovalView, wait_seconds: float) -> str:
        """Poll every 3 s until a decision, expiry or the end of the wait."""
        return wait_for_approval(
            read=lambda: self.read_approval(view.id),
            initial=view,
            wait_seconds=wait_seconds,
            sleeper=self._sleeper,
            clock=self._clock,
        )

    @contextmanager
    def stream(self, method: str, path: str, **kwargs: Any) -> Iterator[httpx.Response]:
        """Yield an unread successful response with the normal auth/retry policy.

        Retries happen only before yielding bytes; an interrupted download is never
        replayed into stdout. Error bodies are consumed for structured diagnostics.
        """
        response = self.request(method, path, _stream=True, **kwargs)
        try:
            yield response
        except httpx.HTTPError:
            raise ApiError(
                status_code=None,
                code="transport_error",
                message="Download interrupted; output may be incomplete.",
                hint=None,
                detail=None,
            ) from None
        finally:
            response.close()


def wait_for_approval(
    *,
    read: Callable[[], ApprovalView],
    initial: ApprovalView,
    wait_seconds: float,
    sleeper: Callable[[float], object],
    clock: Callable[[], datetime],
) -> str:
    """Return the approval status after polling every 3 s within the wait."""
    view = initial
    started = clock()
    while True:
        status = view.status
        if status == "pending" and view.expires_at <= clock():
            status = "expired"
        if status != "pending":
            return status
        waited = (clock() - started).total_seconds()
        if waited + APPROVAL_POLL_SECONDS > wait_seconds:
            return "pending"
        sleeper(APPROVAL_POLL_SECONDS)
        view = read()
