"""Synchronous HTTP client, authentication, and bounded retry tests."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from login_fixtures import login_credential

from solwyn_cli import auth
from solwyn_cli import client as client_module
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import ApiError

VALID_KEY = "solwyn_sk_" + "a1" * 32
API_URL = "https://api.solwyn.ai/prefix"


def _project_key() -> auth.ApiKeyCredential:
    return auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")


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
    assert request.headers["User-Agent"] == "solwyn-cli/0.5.0"
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
            "POST", "/oauth/token", json={"email": "person@example.com"}
        )

    assert response.json() == {"ok": True}
    assert len(requests) == 1
    request = requests[0]
    assert "Authorization" not in request.headers
    assert request.headers["User-Agent"] == "solwyn-cli/0.5.0"
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
            client.request_unauthenticated("POST", "/oauth/token", json={})

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
    resolved: list[auth.ApiKeyCredential] = []

    def resolve() -> auth.ApiKeyCredential:
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

    def resolve() -> auth.ApiKeyCredential:
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

    def resolve() -> auth.ApiKeyCredential:
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


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unexplained_401_is_an_auth_error_and_never_replayed(method: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"detail": "Expired."})

    with SolwynClient(
        api_url=API_URL,
        credential=login_credential("login-secret"),
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(auth.AuthError) as exc_info:
            client.request(method, "/api/v1/projects")

    assert str(exc_info.value) == "Authentication failed. Check your credentials."
    assert len(requests) == 1
    assert "login-secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        ("cli_credential_invalid", "Your Solwyn login is not valid. Run solwyn login."),
        ("cli_credential_expired", "Your Solwyn login expired. Run solwyn login."),
        ("cli_credential_revoked", "Your Solwyn login was revoked. Run solwyn login."),
    ],
)
def test_cli_credential_401_codes_are_explained(code: str, hint: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            json={"detail": {"code": code, "message": "Login rejected."}},
        )

    with SolwynClient(
        api_url=API_URL,
        credential=login_credential(),
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("GET", "/api/v1/projects")

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == code
    assert exc_info.value.hint == hint


def test_approval_limit_reached_is_not_retried_or_renamed() -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={
                "detail": {
                    "code": "approval_limit_reached",
                    "message": "Too many pending approvals.",
                }
            },
        )

    with SolwynClient(
        api_url=API_URL,
        credential=login_credential(),
        transport=httpx.MockTransport(handle),
        sleeper=lambda seconds: None,
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("PUT", "/api/v1/projects/p/budget", json={})

    assert attempts == 1
    assert exc_info.value.code == "approval_limit_reached"
    assert "pending approvals" in (exc_info.value.hint or "")
