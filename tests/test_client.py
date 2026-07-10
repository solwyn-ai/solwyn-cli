"""Synchronous HTTP client, authentication, and bounded retry tests."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from solwyn_cli import auth
from solwyn_cli import client as client_module
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import ApiError


VALID_KEY = "sk_proj_" + "a1" * 32
API_URL = "https://api.solwyn.ai/prefix"


def _project_key() -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


def test_success_uses_normalized_base_url_auth_user_agent_and_exact_timeouts(
    requests: list[httpx.Request],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with SolwynClient(
        api_url=f"{API_URL}/",
        credential=_project_key(),
        transport=httpx.MockTransport(handle),
    ) as client:
        response = client.request("GET", "/api/v1/costs", params={"limit": 1})

    assert response.json() == {"ok": True}
    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL(f"{API_URL}/api/v1/costs?limit=1")
    assert request.headers["Authorization"] == f"Bearer {VALID_KEY}"
    assert request.headers["User-Agent"] == "solwyn-cli/0.1.0"
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 5.0,
    }


def test_context_manager_and_explicit_close_close_the_underlying_client() -> None:
    client = SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(lambda request: httpx.Response(204)),
    )
    assert client.is_closed is False

    with client as entered:
        assert entered is client
        assert client.is_closed is False

    assert client.is_closed is True

    second = SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(lambda request: httpx.Response(204)),
    )
    second.close()
    assert second.is_closed is True


def test_credentials_are_resolved_at_each_logical_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[auth.ProjectKeyCredential] = []

    def resolve() -> auth.ProjectKeyCredential:
        credential = _project_key()
        resolved.append(credential)
        return credential

    monkeypatch.setattr(client_module, "resolve_credential", resolve)
    transport = httpx.MockTransport(lambda request: httpx.Response(204))

    with SolwynClient(api_url=API_URL, transport=transport) as client:
        client.request("GET", "api/v1/projects")
        client.request("GET", "api/v1/projects")

    assert len(resolved) == 2


def test_missing_credential_is_a_safe_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "resolve_credential", lambda: None)

    with SolwynClient(
        api_url=API_URL,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("request sent without credentials")
        ),
    ) as client:
        with pytest.raises(auth.AuthError) as exc_info:
            client.request("GET", "api/v1/projects")

    assert exc_info.value.exit_code == 3


def test_absolute_request_url_is_rejected_without_sending_credentials() -> None:
    external_url = "https://attacker.example/collect"

    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(
            lambda request: pytest.fail("absolute request URL reached the transport")
        ),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", external_url)

    assert exc_info.value.code == "invalid_request"
    assert external_url not in str(exc_info.value)
    assert VALID_KEY not in str(exc_info.value)


def test_jwt_refresh_uses_client_user_agent_and_timeout_then_retries_once(
    requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = auth.JwtCredential(
        access_token="old-access-secret", refresh_token="old-refresh-secret"
    )

    def store(access_token: str, refresh_token: str) -> auth.JwtCredential:
        return auth.JwtCredential(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    monkeypatch.setattr(auth, "store_jwt_pair", store)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/refresh"):
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access-secret",
                    "refresh_token": "new-refresh-secret",
                    "token_type": "bearer",
                },
            )
        authorization = request.headers.get("Authorization")
        return httpx.Response(
            401 if authorization == "Bearer old-access-secret" else 200,
            json={"ok": True},
        )

    with SolwynClient(
        api_url=API_URL,
        credential=jwt,
        transport=httpx.MockTransport(handle),
    ) as client:
        response = client.request("GET", "api/v1/costs")

    assert response.status_code == 200
    assert len(requests) == 3
    refresh = requests[1]
    assert refresh.url == httpx.URL(f"{API_URL}/api/v1/auth/refresh")
    assert refresh.headers["User-Agent"] == "solwyn-cli/0.1.0"
    assert "Authorization" not in refresh.headers
    assert refresh.extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 5.0,
    }


@pytest.mark.parametrize(
    ("retry_after", "now", "expected_wait"),
    [
        ("3", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 3.0),
        ("99", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 10.0),
        ("-4", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 0.0),
        (
            format_datetime(
                datetime(2026, 7, 10, 12, 0, tzinfo=UTC) + timedelta(seconds=4)
            ),
            datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            4.0,
        ),
        ("not-a-delay", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 0.0),
    ],
)
def test_429_retries_once_with_bounded_retry_after_wait(
    retry_after: str,
    now: datetime,
    expected_wait: float,
) -> None:
    attempts = 0
    waits: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after},
                json={"detail": "Slow down."},
            )
        return httpx.Response(200, json={"ok": True})

    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(handle),
        sleeper=waits.append,
        clock=lambda: now,
    ) as client:
        response = client.request("GET", "api/v1/costs")

    assert response.status_code == 200
    assert attempts == 2
    assert waits == [expected_wait]


def test_second_429_becomes_structured_error_without_retry_loop() -> None:
    attempts = 0
    waits: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"detail": "Still limited."})

    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(handle),
        sleeper=waits.append,
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", "api/v1/costs")

    assert attempts == 2
    assert waits == [0.0]
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.message == "Still limited."
    assert exc_info.value.detail == "Still limited."


def test_one_429_retry_is_shared_across_a_jwt_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = auth.JwtCredential(
        access_token="old-access-secret", refresh_token="old-refresh-secret"
    )
    ordinary_attempts = 0
    refresh_attempts = 0
    waits: list[float] = []

    def store(access_token: str, refresh_token: str) -> auth.JwtCredential:
        return auth.JwtCredential(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    monkeypatch.setattr(auth, "store_jwt_pair", store)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal ordinary_attempts, refresh_attempts
        if request.url.path.endswith("/auth/refresh"):
            refresh_attempts += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access-secret",
                    "refresh_token": "new-refresh-secret",
                },
            )
        ordinary_attempts += 1
        statuses = [429, 401, 429]
        return httpx.Response(
            statuses[ordinary_attempts - 1],
            headers={"Retry-After": "1"},
            json={"detail": "bounded"},
        )

    with SolwynClient(
        api_url=API_URL,
        credential=jwt,
        transport=httpx.MockTransport(handle),
        sleeper=waits.append,
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", "api/v1/costs")

    assert exc_info.value.code == "rate_limited"
    assert ordinary_attempts == 3
    assert refresh_attempts == 1
    assert waits == [1.0]


def test_transport_failure_becomes_safe_typed_error_without_secret_leak() -> None:
    transport_message = "network failed near provider-secret-value"

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(transport_message, request=request)

    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("POST", "api/v1/costs", json={"prompt": "private"})

    error = exc_info.value
    assert error.status_code is None
    assert error.code == "transport_error"
    assert error.detail is None
    assert "provider-secret-value" not in str(error)
    assert "provider-secret-value" not in repr(error)
    assert "private" not in str(error)


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_code", "expected_detail"),
    [
        (400, {"detail": "Bad input."}, "http_error", "Bad input."),
        (
            402,
            {
                "detail": {
                    "code": "quota_exceeded",
                    "message": "Quota used.",
                    "cta_path": "/billing",
                }
            },
            "quota_exceeded",
            {
                "code": "quota_exceeded",
                "message": "Quota used.",
                "cta_path": "/billing",
            },
        ),
        (
            418,
            {"detail": {"code": "future", "message": "Future error."}},
            "future",
            {"code": "future", "message": "Future error."},
        ),
    ],
)
def test_non_success_responses_dispatch_detail_shapes(
    status_code: int,
    payload: Any,
    expected_code: str,
    expected_detail: str | dict[str, object],
) -> None:
    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json=payload)
        ),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", "api/v1/costs")

    assert exc_info.value.code == expected_code
    assert exc_info.value.detail == expected_detail
