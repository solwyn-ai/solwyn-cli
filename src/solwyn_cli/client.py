"""Synchronous authenticated HTTP client with bounded retries."""

import time
from collections.abc import Callable, Mapping, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import httpx

from solwyn_cli import __version__
from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ResolvedCredential,
    request_with_refresh_retry,
    resolve_credential,
)
from solwyn_cli.errors import (
    BILLING_HINTS,
    BILLING_WRITE_UNKNOWN_HINT,
    ApiError,
    BUDGET_BUSY_HINT,
    INGEST_BUSY_HINT,
    INGEST_UNKNOWN_HINT,
    LEGACY_BATCH_BUSY_HINT,
    SERVICE_BUSY_HINT,
    WRITE_BUSY_HINT,
    error_from_response,
    is_budget_admission_busy,
    is_ingest_unaccepted,
)


CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
WRITE_TIMEOUT_SECONDS = 30.0
POOL_TIMEOUT_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 10.0

_UNSET = object()


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
        """Return the credential currently retained after any JWT rotation."""
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
        method: str,
        path: str,
        headers: httpx.Headers,
        kwargs: dict[str, Any],
        evidence: _RequestEvidence,
        retry_rate_limit: bool = True,
        stream: bool = False,
    ) -> httpx.Response:
        """Share one bounded 429/eligible-503 retry across JWT rotation.

        retry_rate_limit controls only 429; eligible 503 retries remain enabled.
        """

        def send() -> httpx.Response:
            if not stream:
                return self._client.request(method, path, headers=headers, **kwargs)
            request = self._client.build_request(
                method, path, headers=headers, **kwargs
            )
            response = self._client.send(request, stream=True)
            if not response.is_success:
                try:
                    response.read()
                finally:
                    response.close()
            return response

        response = send()
        while True:
            endpoint = urlsplit(path).path.rstrip("/")
            retryable = False
            if retry_rate_limit and response.status_code == 429:
                retryable = True
            elif response.status_code == 503:
                safe_busy_retry = "Retry-After" in response.headers and (
                    method.upper() == "GET"
                    or method.upper() == "POST"
                    and endpoint == "api/v1/metadata/ingest"
                )
                login_capacity = (
                    method.upper() == "POST"
                    and endpoint == "api/v1/auth/login"
                    and error_from_response(response).code == "auth_capacity_exceeded"
                )
                if safe_busy_retry or login_capacity:
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
            error = error_from_response(
                response,
                code_override=(
                    "rate_limited"
                    if response.status_code == httpx.codes.TOO_MANY_REQUESTS
                    else None
                ),
            )
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
                method=method,
                path=relative_path,
                headers=request_headers,
                kwargs=kwargs,
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
        retry_auth: bool = True,
        _stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make one logical request with bounded capacity and auth retries.

        retry_rate_limit=False disables 429 retries only; eligible GET/ingest
        503 responses still share the single capacity retry budget.
        """
        relative_path, request_headers = self._prepare_request(path, kwargs)
        credential = self._resolve_credential()
        evidence = _RequestEvidence()

        def retain_rotated_credential(rotated: JwtCredential) -> None:
            if self._credential is not _UNSET:
                self._credential = rotated

        def send(resolved: ResolvedCredential) -> httpx.Response:
            headers = request_headers.copy()
            headers["Authorization"] = f"Bearer {resolved.bearer_value()}"
            return self._send_with_capacity_retry(
                method=method,
                path=relative_path,
                headers=headers,
                kwargs=kwargs,
                evidence=evidence,
                retry_rate_limit=retry_rate_limit,
                stream=_stream,
            )

        try:
            response = (
                request_with_refresh_retry(
                    client=self._client,
                    api_url=self._api_url,
                    credential=credential,
                    request=send,
                    on_credential_rotated=retain_rotated_credential,
                )
                if retry_auth
                else send(credential)
            )
        except AuthError as error:
            error.ingest_outcome_uncertain = evidence.ingest_outcome_uncertain
            raise
        except httpx.HTTPError:
            raise ApiError(
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
            ) from None

        self._raise_for_unsuccessful_response(
            response, method=method, path=relative_path, evidence=evidence
        )
        return response

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
