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

    def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make one logical request with bounded rate-limit and auth retries."""
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
        credential = self._resolve_credential()
        retried_rate_limit = False
        request_headers = httpx.Headers(kwargs.pop("headers", None))
        request_headers["User-Agent"] = f"solwyn-cli/{__version__}"

        def send(resolved: ResolvedCredential) -> httpx.Response:
            nonlocal retried_rate_limit
            headers = request_headers.copy()
            headers["Authorization"] = f"Bearer {resolved.bearer_value()}"
            response = self._client.request(
                method,
                path.lstrip("/"),
                headers=headers,
                **kwargs,
            )
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                if retried_rate_limit:
                    return response
                retried_rate_limit = True
                delay = _retry_after_seconds(
                    response.headers.get("Retry-After"), self._clock()
                )
                self._sleeper(delay)
                response = self._client.request(
                    method,
                    path.lstrip("/"),
                    headers=headers,
                    **kwargs,
                )
            return response

        try:
            response = request_with_refresh_retry(
                client=self._client,
                api_url=self._api_url,
                credential=credential,
                request=send,
            )
        except httpx.HTTPError:
            raise ApiError(
                status_code=None,
                code="transport_error",
                message="Could not connect to the Solwyn API.",
                hint=None,
                detail=None,
            ) from None

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise error_from_response(response, code_override="rate_limited")
        if not response.is_success:
            raise error_from_response(response)
        return response
