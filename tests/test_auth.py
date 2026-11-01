"""Credential resolution by command class; store behaviour is in test_credential_store."""

import json

from datetime import UTC, datetime, timedelta

import pytest

from login_fixtures import (
    DictStore,
    fake_keychain,
    install_store,
    LOGIN_TOKEN,
    expired_login_entries,
    login_entries,
    login_meta,
)
from solwyn_cli import auth


VALID_KEY = "sk_proj_" + "a1" * 32
ENV_KEY = "sk_proj_" + "e1" * 32
DEVICE_CODE = "solwyn_dc_" + "0f" * 32


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    return fake_keychain(monkeypatch)


def _kind(credential: auth.ResolvedCredential | None) -> str | None:
    if credential is None:
        return None
    if isinstance(credential, auth.LoginCredential):
        return "login"
    return f"key:{credential.source}"


# (stored login, environment key, stored key) -> resolved credential
_USER_ORDER = [
    ((True, True, True), "login"),
    ((True, False, True), "login"),
    ((True, True, False), "login"),
    ((True, False, False), "login"),
    ((False, True, True), "key:environment"),
    ((False, False, True), "key:file"),
    ((False, True, False), "key:environment"),
    ((False, False, False), None),
]


def _setup(
    keychain: dict[str, str], login: bool, env_key: bool, stored_key: bool
) -> dict[str, str]:
    if login:
        keychain.update(login_entries())
    if stored_key:
        keychain[auth.API_KEY_ENTRY] = VALID_KEY
    return {"SOLWYN_API_KEY": ENV_KEY} if env_key else {}


@pytest.mark.parametrize(("present", "expected"), _USER_ORDER)
def test_user_class_prefers_login_then_environment_key_then_stored_key(
    keychain: dict[str, str], present: tuple[bool, bool, bool], expected: str | None
) -> None:
    environ = _setup(keychain, *present)

    assert _kind(auth.resolve_credential(environ)) == expected


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ((True, True, True), "key:environment"),
        ((True, False, True), "key:file"),
        ((True, True, False), "key:environment"),
        ((True, False, False), None),
        ((False, False, False), None),
    ],
)
def test_metering_class_never_uses_the_login(
    keychain: dict[str, str], present: tuple[bool, bool, bool], expected: str | None
) -> None:
    environ = _setup(keychain, *present)
    environ["SOLWYN_AUTH"] = "login"

    assert _kind(auth.resolve_credential(environ, purpose="metering")) == expected


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ((True, True, True), "key:environment"),
        ((True, False, True), "key:file"),
        ((True, False, False), None),
    ],
)
def test_solwyn_auth_project_key_skips_the_login(
    keychain: dict[str, str], present: tuple[bool, bool, bool], expected: str | None
) -> None:
    environ = _setup(keychain, *present)
    environ["SOLWYN_AUTH"] = "project_key"

    assert _kind(auth.resolve_credential(environ)) == expected


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ((True, True, True), "login"),
        ((False, True, True), None),
        ((False, False, True), None),
    ],
)
def test_solwyn_auth_login_requires_the_login_without_fallback(
    keychain: dict[str, str], present: tuple[bool, bool, bool], expected: str | None
) -> None:
    environ = _setup(keychain, *present)
    environ["SOLWYN_AUTH"] = "login"

    assert _kind(auth.resolve_credential(environ)) == expected


@pytest.mark.parametrize("value", ["jwt", "LOGIN", "key", " login"])
def test_unknown_solwyn_auth_is_rejected(keychain: dict[str, str], value: str) -> None:
    with pytest.raises(ValueError):
        auth.auth_selection({"SOLWYN_AUTH": value})
    with pytest.raises(auth.AuthError):
        auth.resolve_credential({"SOLWYN_AUTH": value})


def test_empty_solwyn_auth_means_default_order(keychain: dict[str, str]) -> None:
    keychain.update(login_entries())

    assert auth.auth_selection({"SOLWYN_AUTH": ""}) is None
    assert _kind(auth.resolve_credential({"SOLWYN_AUTH": ""})) == "login"


def test_expired_local_login_counts_as_absent(keychain: dict[str, str]) -> None:
    keychain.update(expired_login_entries())
    keychain[auth.API_KEY_ENTRY] = VALID_KEY

    assert _kind(auth.resolve_credential({})) == "key:file"
    assert auth.expired_login_date() is not None
    assert auth.stored_login() is None
    assert auth.stored_login(include_expired=True) is not None


def test_unexpired_login_reports_no_expiry(keychain: dict[str, str]) -> None:
    keychain.update(login_entries())

    assert auth.expired_login_date() is None


@pytest.mark.parametrize(
    "entries",
    [
        {auth.LOGIN_ENTRY: json.dumps({"token": LOGIN_TOKEN})},
        {auth.LOGIN_ENTRY: json.dumps({"meta": login_meta().model_dump(mode="json")})},
        {
            auth.LOGIN_ENTRY: json.dumps(
                {
                    "token": "solwyn_cli_short",
                    "meta": login_meta().model_dump(mode="json"),
                }
            )
        },
        {auth.LOGIN_ENTRY: "{"},
    ],
)
def test_incomplete_or_corrupt_login_is_a_structured_error(
    keychain: dict[str, str], entries: dict[str, str]
) -> None:
    # Never skipped: skipping would fall through to a stored project key.
    keychain.update(entries)
    keychain[auth.API_KEY_ENTRY] = VALID_KEY

    with pytest.raises(auth.MalformedCredentialFileError):
        auth.resolve_credential({})


def test_invalid_environment_key_fails_without_echoing_it(
    keychain: dict[str, str],
) -> None:
    invalid_key = "not-a-project-key"

    with pytest.raises(auth.AuthError) as exc_info:
        auth.resolve_credential({"SOLWYN_API_KEY": invalid_key})

    assert exc_info.value.exit_code == 3
    assert invalid_key not in str(exc_info.value)
    assert invalid_key not in repr(exc_info.value)


def test_login_masks_an_invalid_environment_key(keychain: dict[str, str]) -> None:
    keychain.update(login_entries())

    assert _kind(auth.resolve_credential({"SOLWYN_API_KEY": "broken"})) == "login"


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


def test_store_login_rejects_a_malformed_token(keychain: dict[str, str]) -> None:
    with pytest.raises(auth.CredentialStorageError) as exc_info:
        auth.store_login("solwyn_cli_not-hex", login_meta())

    assert "not-hex" not in str(exc_info.value)
    assert keychain == {}


def test_pending_device_round_trips_and_clears(keychain: dict[str, str]) -> None:
    pending = auth.PendingDevice(
        device_code=DEVICE_CODE,
        user_code="BCDF-GHJK",
        verification_uri="https://app.solwyn.ai/device",
        verification_uri_complete="https://app.solwyn.ai/device?user_code=BCDF-GHJK",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        interval=5,
    )

    auth.store_pending_device(pending)
    loaded = auth.load_pending_device()
    auth.clear_pending_device()

    assert loaded is not None
    assert loaded.device_code.get_secret_value() == DEVICE_CODE
    assert DEVICE_CODE not in repr(loaded)
    assert auth.load_pending_device() is None


def test_clear_login_keeps_the_project_key(keychain: dict[str, str]) -> None:
    keychain.update(login_entries())
    keychain[auth.API_KEY_ENTRY] = VALID_KEY

    auth.clear_login()

    assert keychain == {auth.API_KEY_ENTRY: VALID_KEY}


def test_secret_models_are_redacted() -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="keyring")
    login = auth.LoginCredential(token=LOGIN_TOKEN, meta=login_meta())

    assert VALID_KEY not in repr(key)
    assert LOGIN_TOKEN not in repr(login)
    assert LOGIN_TOKEN not in str(login.model_dump())


def test_login_model_enforces_the_token_format_without_echoing_it() -> None:
    with pytest.raises(ValueError) as exc_info:
        auth.LoginCredential(token="secret-session-jwt", meta=login_meta())

    assert "secret-session-jwt" not in str(exc_info.value)


def test_project_key_model_enforces_format_without_echoing_secret() -> None:
    with pytest.raises(ValueError) as exc_info:
        auth.ProjectKeyCredential(api_key="sk_proj_short", source="keyring")

    assert "sk_proj_short" not in str(exc_info.value)


NEW_TOKEN = "solwyn_cli_" + "f7" * 32


def test_metering_never_uses_a_stored_login(monkeypatch: pytest.MonkeyPatch) -> None:
    # The file is read whole; metering still selects only the project key.
    install_store(
        monkeypatch, DictStore({**login_entries(), auth.API_KEY_ENTRY: VALID_KEY})
    )

    found = auth.resolve_credential({}, purpose="metering")

    assert isinstance(found, auth.ProjectKeyCredential)
    assert found.bearer_value() == VALID_KEY
