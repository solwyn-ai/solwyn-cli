"""Credential resolution, keychain safety, and refresh retry tests."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from keyring.errors import NoKeyringError

from solwyn_cli import auth


VALID_KEY = "sk_proj_" + "a1" * 32


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values: dict[str, str] = {}

    monkeypatch.setattr(
        auth.keyring,
        "get_password",
        lambda service, name: values.get(name),
    )
    monkeypatch.setattr(
        auth.keyring,
        "set_password",
        lambda service, name, value: values.__setitem__(name, value),
    )

    def delete_password(service: str, name: str) -> None:
        values.pop(name, None)

    monkeypatch.setattr(auth.keyring, "delete_password", delete_password)
    return values


def test_environment_key_wins_without_touching_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object) -> None:
        pytest.fail("environment credential resolution touched keyring")

    monkeypatch.setattr(auth.keyring, "get_password", fail_if_called)

    credential = auth.resolve_credential({"SOLWYN_API_KEY": VALID_KEY})

    assert isinstance(credential, auth.ProjectKeyCredential)
    assert credential.source == "environment"
    assert credential.bearer_value() == VALID_KEY


def test_invalid_environment_key_fails_without_falling_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_key = "not-a-project-key"

    def fail_if_called(*args: object) -> None:
        pytest.fail("invalid environment credential touched keyring")

    monkeypatch.setattr(auth.keyring, "get_password", fail_if_called)

    with pytest.raises(auth.AuthError) as exc_info:
        auth.resolve_credential({"SOLWYN_API_KEY": invalid_key})

    assert exc_info.value.exit_code == 3
    assert invalid_key not in str(exc_info.value)
    assert invalid_key not in repr(exc_info.value)


def test_keychain_api_key_wins_over_complete_jwt_pair(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain.update(
        api_key=VALID_KEY,
        access_token="access-secret",
        refresh_token="refresh-secret",
    )
    reads: list[str] = []

    def get_password(service: str, name: str) -> str | None:
        assert service == auth.KEYRING_SERVICE
        reads.append(name)
        return keychain.get(name)

    monkeypatch.setattr(auth.keyring, "get_password", get_password)

    credential = auth.resolve_credential({})

    assert isinstance(credential, auth.ProjectKeyCredential)
    assert credential.source == "keyring"
    assert credential.bearer_value() == VALID_KEY
    assert reads == [auth.API_KEY_ENTRY]


def test_complete_keychain_jwt_pair_is_resolved(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain.update(access_token="access-secret", refresh_token="refresh-secret")
    reads: list[str] = []

    def get_password(service: str, name: str) -> str | None:
        reads.append(name)
        return keychain.get(name)

    monkeypatch.setattr(auth.keyring, "get_password", get_password)

    credential = auth.resolve_credential({})

    assert isinstance(credential, auth.JwtCredential)
    assert credential.bearer_value() == "access-secret"
    assert credential.refresh_value() == "refresh-secret"
    assert reads == [
        auth.API_KEY_ENTRY,
        auth.ACCESS_TOKEN_ENTRY,
        auth.REFRESH_TOKEN_ENTRY,
    ]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ({}, None),
        ({"access_token": "access-secret"}, None),
        ({"refresh_token": "refresh-secret"}, None),
        ({"access_token": "", "refresh_token": "refresh-secret"}, None),
        ({"access_token": "access-secret", "refresh_token": ""}, None),
    ],
)
def test_partial_or_absent_keychain_jwt_pair_is_not_a_credential(
    entries: dict[str, str], expected: None, keychain: dict[str, str]
) -> None:
    keychain.update(entries)

    assert auth.resolve_credential({}) is expected


def test_invalid_stored_api_key_fails_without_reading_jwt(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_key = "stored-invalid-secret"
    keychain.update(
        api_key=invalid_key,
        access_token="access-secret",
        refresh_token="refresh-secret",
    )
    reads: list[str] = []

    def get_password(service: str, name: str) -> str | None:
        reads.append(name)
        return keychain.get(name)

    monkeypatch.setattr(auth.keyring, "get_password", get_password)

    with pytest.raises(auth.AuthError) as exc_info:
        auth.resolve_credential({})

    assert exc_info.value.exit_code == 3
    assert invalid_key not in str(exc_info.value)
    assert reads == [auth.API_KEY_ENTRY]


@pytest.mark.parametrize("backend_error", [NoKeyringError(), RuntimeError("down")])
def test_unavailable_keyring_is_a_safe_no_credential_outcome(
    backend_error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_get(service: str, name: str) -> str | None:
        raise backend_error

    monkeypatch.setattr(auth.keyring, "get_password", fail_get)

    assert auth.resolve_credential({}) is None


def test_store_project_key_validates_and_writes_only_to_keyring(
    keychain: dict[str, str],
) -> None:
    credential = auth.store_project_key(VALID_KEY)

    assert credential.bearer_value() == VALID_KEY
    assert keychain == {auth.API_KEY_ENTRY: VALID_KEY}


def test_store_project_key_rejects_invalid_secret_without_echoing_it(
    keychain: dict[str, str],
) -> None:
    invalid_key = "invalid-secret"

    with pytest.raises(auth.AuthError) as exc_info:
        auth.store_project_key(invalid_key)

    assert invalid_key not in str(exc_info.value)
    assert keychain == {}


def test_store_and_rotate_jwt_pair(keychain: dict[str, str]) -> None:
    auth.store_jwt_pair("access-one", "refresh-one")
    rotated = auth.store_jwt_pair("access-two", "refresh-two")

    assert keychain == {
        auth.ACCESS_TOKEN_ENTRY: "access-two",
        auth.REFRESH_TOKEN_ENTRY: "refresh-two",
    }
    assert rotated.bearer_value() == "access-two"
    assert rotated.refresh_value() == "refresh-two"


def test_jwt_rotation_writes_refresh_before_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []

    def set_password(service: str, name: str, value: str) -> None:
        writes.append(name)

    monkeypatch.setattr(auth.keyring, "set_password", set_password)

    auth.store_jwt_pair("new-access", "new-refresh")

    assert writes == [auth.REFRESH_TOKEN_ENTRY, auth.ACCESS_TOKEN_ENTRY]


def test_clear_credentials_deletes_all_entries(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain.update(
        api_key=VALID_KEY,
        access_token="access-secret",
        refresh_token="refresh-secret",
    )
    deletes: list[str] = []

    def delete_password(service: str, name: str) -> None:
        assert service == auth.KEYRING_SERVICE
        deletes.append(name)
        keychain.pop(name, None)

    monkeypatch.setattr(auth.keyring, "delete_password", delete_password)

    auth.clear_credentials()

    assert keychain == {}
    assert deletes == [
        auth.API_KEY_ENTRY,
        auth.ACCESS_TOKEN_ENTRY,
        auth.REFRESH_TOKEN_ENTRY,
    ]


@pytest.mark.parametrize("operation", ["set_password", "delete_password"])
@pytest.mark.parametrize("backend_error", [NoKeyringError(), RuntimeError("down")])
def test_keychain_write_failures_are_safe_exit_three_errors(
    operation: str,
    backend_error: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object) -> None:
        raise backend_error

    monkeypatch.setattr(auth.keyring, operation, fail)

    with pytest.raises(auth.AuthError) as exc_info:
        if operation == "set_password":
            auth.store_project_key(VALID_KEY)
        else:
            auth.clear_credentials()

    assert exc_info.value.exit_code == 3
    assert "down" not in str(exc_info.value)


def test_secret_models_and_validation_errors_are_redacted() -> None:
    api_key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    jwt = auth.JwtCredential(
        access_token="access-secret", refresh_token="refresh-secret"
    )

    for rendered in (repr(api_key), str(api_key), repr(jwt), str(jwt)):
        assert VALID_KEY not in rendered
        assert "access-secret" not in rendered
        assert "refresh-secret" not in rendered

    with pytest.raises(Exception) as exc_info:
        auth.JwtCredential(access_token="access-secret", refresh_token="")

    assert "access-secret" not in str(exc_info.value)


def test_project_key_model_enforces_format_without_echoing_secret() -> None:
    invalid_key = "direct-invalid-secret"

    with pytest.raises(Exception) as exc_info:
        auth.ProjectKeyCredential(api_key=invalid_key, source="environment")

    assert invalid_key not in str(exc_info.value)


def _response(status_code: int, payload: Any | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://api.solwyn.ai/resource"),
    )


@pytest.fixture
def jwt_credential() -> auth.JwtCredential:
    return auth.JwtCredential(access_token="old-access", refresh_token="old-refresh")


@pytest.fixture
def refresh_calls() -> list[httpx.Request]:
    return []


@pytest.fixture
def refresh_client(refresh_calls: list[httpx.Request]) -> Iterator[httpx.Client]:
    def handle(request: httpx.Request) -> httpx.Response:
        refresh_calls.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "bearer",
                "future_response_field": "accepted",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        yield client


def test_jwt_401_refreshes_rotates_and_retries_exactly_once(
    jwt_credential: auth.JwtCredential,
    refresh_client: httpx.Client,
    refresh_calls: list[httpx.Request],
    keychain: dict[str, str],
) -> None:
    attempts: list[auth.JwtCredential] = []

    def request(credential: auth.ResolvedCredential) -> httpx.Response:
        assert isinstance(credential, auth.JwtCredential)
        attempts.append(credential)
        return _response(401 if len(attempts) == 1 else 200)

    response = auth.request_with_refresh_retry(
        client=refresh_client,
        api_url="https://api.solwyn.ai/",
        credential=jwt_credential,
        request=request,
    )

    assert response.status_code == 200
    assert len(attempts) == 2
    assert attempts[0].bearer_value() == "old-access"
    assert attempts[1].bearer_value() == "new-access"
    assert len(refresh_calls) == 1
    assert refresh_calls[0].url == httpx.URL(
        "https://api.solwyn.ai/api/v1/auth/refresh"
    )
    assert refresh_calls[0].method == "POST"
    assert refresh_calls[0].read() == b'{"refresh_token":"old-refresh"}'
    assert keychain == {
        auth.ACCESS_TOKEN_ENTRY: "new-access",
        auth.REFRESH_TOKEN_ENTRY: "new-refresh",
    }


def test_second_401_exits_three_without_a_refresh_loop(
    jwt_credential: auth.JwtCredential,
    refresh_client: httpx.Client,
    refresh_calls: list[httpx.Request],
    keychain: dict[str, str],
) -> None:
    attempts = 0

    def request(credential: auth.ResolvedCredential) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response(401)

    with pytest.raises(auth.AuthError) as exc_info:
        auth.request_with_refresh_retry(
            client=refresh_client,
            api_url="https://api.solwyn.ai",
            credential=jwt_credential,
            request=request,
        )

    assert exc_info.value.exit_code == 3
    assert attempts == 2
    assert len(refresh_calls) == 1


def test_api_key_401_never_refreshes(
    refresh_client: httpx.Client, refresh_calls: list[httpx.Request]
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    attempts = 0

    def request(resolved: auth.ResolvedCredential) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response(401)

    with pytest.raises(auth.AuthError) as exc_info:
        auth.request_with_refresh_retry(
            client=refresh_client,
            api_url="https://api.solwyn.ai",
            credential=credential,
            request=request,
        )

    assert exc_info.value.exit_code == 3
    assert attempts == 1
    assert refresh_calls == []


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (500, {"detail": "refresh-secret-must-not-leak"}),
        (200, {"access_token": "new-access"}),
        (200, {"access_token": "", "refresh_token": "new-refresh"}),
        (200, ["not", "a", "token", "response"]),
    ],
)
def test_failed_or_malformed_refresh_exits_three_without_retrying(
    status_code: int,
    payload: Any,
    jwt_credential: auth.JwtCredential,
) -> None:
    refresh_requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        refresh_requests.append(request)
        return httpx.Response(status_code, json=payload)

    attempts = 0

    def request(credential: auth.ResolvedCredential) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response(401)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(auth.AuthError) as exc_info:
            auth.request_with_refresh_retry(
                client=client,
                api_url="https://api.solwyn.ai",
                credential=jwt_credential,
                request=request,
            )

    assert exc_info.value.exit_code == 3
    assert "refresh-secret-must-not-leak" not in str(exc_info.value)
    assert attempts == 1
    assert len(refresh_requests) == 1


def test_refresh_store_failure_exits_three_without_retrying(
    jwt_credential: auth.JwtCredential,
    refresh_client: httpx.Client,
    refresh_calls: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def request(credential: auth.ResolvedCredential) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response(401)

    def fail_store(*args: object) -> None:
        raise NoKeyringError()

    monkeypatch.setattr(auth.keyring, "set_password", fail_store)

    with pytest.raises(auth.AuthError) as exc_info:
        auth.request_with_refresh_retry(
            client=refresh_client,
            api_url="https://api.solwyn.ai",
            credential=jwt_credential,
            request=request,
        )

    assert exc_info.value.exit_code == 3
    assert attempts == 1
    assert len(refresh_calls) == 1
