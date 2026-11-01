"""Suite-wide isolation from the host's credentials.

In-process tests never touch the host's keyring or credentials file. Each test
gets a credentials file under its own temporary directory and an empty
in-memory keyring that is reachable, as on a desktop. Host variables that pick
a credential or a store are removed. A test that needs an unusable keyring
asks for ``keychain_unreachable``.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from solwyn_cli import auth, credential_store


class MemoryKeyring(KeyringBackend):
    """A reachable, empty keychain that lives for one test."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username) from None


@pytest.fixture(autouse=True)
def memory_keyring(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    monkeypatch.setattr(credential_store, "backend_factory", lambda: backend)
    credentials = tmp_path_factory.mktemp("credentials") / "credentials.json"
    monkeypatch.setenv(credential_store.FILE_ENV, str(credentials))
    # The credential lock lives in the per-user state dir; give each test its
    # own (XDG_STATE_HOME reaches child processes; the patch covers macOS).
    state = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(credential_store, "lock_directory", lambda: state / "solwyn")
    monkeypatch.setattr(credential_store, "_STRANDED", False)
    for name in (credential_store.STORE_ENV, auth.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    auth._LAST_LOOKUP.set(None)
    auth._LAST_WRITE.set(None)
    try:
        yield backend
    finally:
        auth._LAST_LOOKUP.set(None)
        auth._LAST_WRITE.set(None)


@pytest.fixture
def credentials_file() -> Path:
    """The isolated credentials file this test reads and writes."""
    return credential_store.credentials_path()


@pytest.fixture
def keychain_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt in: no keyring backend answers, as on a headless runner."""
    monkeypatch.setattr(credential_store, "backend_factory", fail.Keyring)
