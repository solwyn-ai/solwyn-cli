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


def test_unauthenticated_request_has_no_authorization_and_preserves_client_guards(
    requests: list[httpx.Request],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with SolwynClient(
        api_url=API_URL,
        credential=None,
        transport=httpx.MockTransport(handle),
    ) as client:
        response = client.request_unauthenticated(
            "POST", "/api/v1/auth/login", json={"email": "person@example.com"}
        )

    assert response.json() == {"ok": True}
    assert len(requests) == 1
    request = requests[0]
    assert "Authorization" not in request.headers
    assert request.headers["User-Agent"] == "solwyn-cli/0.1.0"
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 5.0,
    }


def test_unauthenticated_request_retries_429_once_then_raises_structured_error() -> (
    None
):
    attempts = 0
    waits: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"detail": "Try later."})

    with SolwynClient(
        api_url=API_URL,
        credential=None,
        transport=httpx.MockTransport(handle),
        sleeper=waits.append,
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request_unauthenticated("POST", "/api/v1/auth/login", json={})

    assert attempts == 2
    assert waits == [0.0]
    assert exc_info.value.code == "rate_limited"


def test_unauthenticated_request_rejects_503_unless_explicitly_accepted() -> None:
    body = {"status": "degraded", "service": "solwyn-api", "checks": []}
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json=body))

    with SolwynClient(
        api_url=API_URL,
        credential=None,
        transport=transport,
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request_unauthenticated("GET", "/health/ready")

    assert exc_info.value.status_code == 503


def test_unauthenticated_request_returns_explicitly_accepted_503_body() -> None:
    body = {
        "status": "degraded",
        "service": "solwyn-api",
        "checks": [{"name": "database", "status": "degraded"}],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json=body))

    with SolwynClient(
        api_url=API_URL,
        credential=None,
        transport=transport,
    ) as client:
        response = client.request_unauthenticated(
            "GET",
            "/health/ready",
            accepted_statuses=frozenset({503}),
        )

    assert response.status_code == 503
    assert response.json() == body


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


@pytest.mark.parametrize(
    "path",
    [
        "../secrets",
        "api/v1/./costs",
        "api/v1/../secrets",
        "%2e%2e/secrets",
        "api%2f..%2fsecrets",
        "api%5c..%5csecrets",
        "%252e%252e%252fsecrets",
    ],
)
def test_traversal_paths_are_rejected_before_credentials_or_transport(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_calls = 0
    transport_calls = 0

    def resolve() -> auth.ProjectKeyCredential:
        nonlocal resolver_calls
        resolver_calls += 1
        return _project_key()

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(204)

    monkeypatch.setattr(client_module, "resolve_credential", resolve)

    with SolwynClient(
        api_url=API_URL,
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", path)

    assert exc_info.value.code == "invalid_request"
    assert path not in str(exc_info.value)
    assert resolver_calls == 0
    assert transport_calls == 0


def test_valid_dot_like_path_and_query_values_preserve_the_base_prefix() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(handle),
    ) as client:
        response = client.request(
            "GET",
            "api/.well-known?next=../valid&encoded=%2e%2e%2fvalid",
        )

    assert response.status_code == 204
    assert len(requests) == 1
    assert requests[0].url.path == "/prefix/api/.well-known"
    assert requests[0].url.params["next"] == "../valid"
    assert requests[0].url.params["encoded"] == "../valid"


@pytest.mark.parametrize(
    "request_options",
    [
        {"timeout": None},
        {"timeout": 0.1},
        {"timeout": httpx.Timeout(1.0)},
        {
            "extensions": {
                "timeout": {
                    "connect": None,
                    "read": None,
                    "write": None,
                    "pool": None,
                }
            }
        },
    ],
)
def test_timeout_overrides_are_rejected_before_credentials_or_transport(
    request_options: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_calls = 0
    transport_calls = 0

    def resolve() -> auth.ProjectKeyCredential:
        nonlocal resolver_calls
        resolver_calls += 1
        return _project_key()

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(204)

    monkeypatch.setattr(client_module, "resolve_credential", resolve)

    with SolwynClient(
        api_url=API_URL,
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", "api/v1/costs", **request_options)

    assert exc_info.value.code == "invalid_request"
    assert resolver_calls == 0
    assert transport_calls == 0


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


def test_explicit_jwt_client_reuses_rotated_pair_on_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = auth.JwtCredential(
        access_token="old-access-secret",
        refresh_token="single-use-refresh-secret",
    )
    authorization_headers: list[str | None] = []
    refresh_attempts = 0

    def store(access_token: str, refresh_token: str) -> auth.JwtCredential:
        return auth.JwtCredential(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    monkeypatch.setattr(auth, "store_jwt_pair", store)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_attempts
        if request.url.path.endswith("/auth/refresh"):
            refresh_attempts += 1
            if refresh_attempts > 1:
                return httpx.Response(
                    401,
                    json={"detail": "Refresh token was already consumed."},
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access-secret",
                    "refresh_token": "new-refresh-secret",
                    "token_type": "bearer",
                },
            )
        authorization = request.headers.get("Authorization")
        authorization_headers.append(authorization)
        if authorization == "Bearer old-access-secret":
            return httpx.Response(401, json={"detail": "Expired access token."})
        return httpx.Response(200, json={"ok": True})

    with SolwynClient(
        api_url=API_URL,
        credential=original,
        transport=httpx.MockTransport(handle),
    ) as client:
        first = client.request("GET", "/api/v1/account/me")
        second = client.request("GET", "/api/v1/projects")

    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}
    assert refresh_attempts == 1
    assert authorization_headers == [
        "Bearer old-access-secret",
        "Bearer new-access-secret",
        "Bearer new-access-secret",
    ]


@pytest.mark.parametrize(
    ("retry_after", "now", "expected_wait"),
    [
        ("3", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 3.0),
        ("99", datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 10.0),
        ("9" * 400, datetime(2026, 7, 10, 12, 0, tzinfo=UTC), 10.0),
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


def test_authenticated_request_can_disable_rate_limit_retry() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={"detail": "Try later."})

    with SolwynClient(
        api_url=API_URL,
        credential=_project_key(),
        transport=httpx.MockTransport(handle),
        sleeper=lambda _seconds: pytest.fail("must not sleep or retry"),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request(
                "POST",
                "/api/v1/budgets/confirm",
                retry_rate_limit=False,
            )

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "rate_limited"
    assert len(requests) == 1
    assert exc_info.value.message == "Try later."
    assert exc_info.value.detail == "Try later."


def test_authenticated_mutation_can_disable_jwt_refresh_replay() -> None:
    jwt = auth.JwtCredential(
        access_token="mutation-access-secret",
        refresh_token="mutation-refresh-secret",
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"detail": "Expired."})

    with SolwynClient(
        api_url=API_URL,
        credential=jwt,
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request(
                "POST",
                "/api/v1/projects",
                retry_rate_limit=False,
                retry_auth=False,
            )

    assert exc_info.value.status_code == 401
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/api/v1/projects")


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
