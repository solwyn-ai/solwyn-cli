"""Synchronous authenticated HTTP client with bounded retries."""

import time
from collections.abc import Callable, Mapping
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
from solwyn_cli.errors import ApiError, error_from_response


CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
WRITE_TIMEOUT_SECONDS = 30.0
POOL_TIMEOUT_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 10.0

_UNSET = object()


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

    def _send_with_rate_limit_retry(
        self,
        *,
        method: str,
        path: str,
        headers: httpx.Headers,
        kwargs: dict[str, Any],
        retry_state: list[bool],
        retry_rate_limit: bool = True,
    ) -> httpx.Response:
        """Send with the one shared bounded 429 retry budget."""
        response = self._client.request(
            method,
            path,
            headers=headers,
            **kwargs,
        )
        if retry_rate_limit and response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            if retry_state[0]:
                return response
            retry_state[0] = True
            delay = _retry_after_seconds(
                response.headers.get("Retry-After"), self._clock()
            )
            self._sleeper(delay)
            response = self._client.request(
                method,
                path,
                headers=headers,
                **kwargs,
            )
        return response

    @staticmethod
    def _raise_for_unsuccessful_response(response: httpx.Response) -> None:
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise error_from_response(response, code_override="rate_limited")
        if not response.is_success:
            raise error_from_response(response)

    def request_unauthenticated(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a guarded request without resolving or sending credentials."""
        relative_path, request_headers = self._prepare_request(path, kwargs)
        try:
            response = self._send_with_rate_limit_retry(
                method=method,
                path=relative_path,
                headers=request_headers,
                kwargs=kwargs,
                retry_state=[False],
            )
        except httpx.HTTPError:
            raise ApiError(
                status_code=None,
                code="transport_error",
                message="Could not connect to the Solwyn API.",
                hint=None,
                detail=None,
            ) from None
        self._raise_for_unsuccessful_response(response)
        return response

    def request(
        self,
        method: str,
        path: str,
        *,
        retry_rate_limit: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make one logical request with bounded rate-limit and auth retries."""
        relative_path, request_headers = self._prepare_request(path, kwargs)
        credential = self._resolve_credential()
        retry_state = [False]

        def retain_rotated_credential(rotated: JwtCredential) -> None:
            if self._credential is not _UNSET:
                self._credential = rotated

        def send(resolved: ResolvedCredential) -> httpx.Response:
            headers = request_headers.copy()
            headers["Authorization"] = f"Bearer {resolved.bearer_value()}"
            return self._send_with_rate_limit_retry(
                method=method,
                path=relative_path,
                headers=headers,
                kwargs=kwargs,
                retry_state=retry_state,
                retry_rate_limit=retry_rate_limit,
            )

        try:
            response = request_with_refresh_retry(
                client=self._client,
                api_url=self._api_url,
                credential=credential,
                request=send,
                on_credential_rotated=retain_rotated_credential,
            )
        except httpx.HTTPError:
            raise ApiError(
                status_code=None,
                code="transport_error",
                message="Could not connect to the Solwyn API.",
                hint=None,
                detail=None,
            ) from None

        self._raise_for_unsuccessful_response(response)
        return response
