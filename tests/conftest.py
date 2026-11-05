"""Suite-wide isolation from the host's credentials.

In-process tests never touch the host's keyring, credentials file or config.
Each test gets a credentials file and a config directory under its own
temporary directories and an empty in-memory keyring that is reachable, as on
a desktop. Host variables that pick
a credential or a store are removed. A test that needs an unusable keyring
asks for ``keychain_unreachable``.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from key_fixtures import fake_core  # noqa: F401 - shared fixture
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from solwyn_cli import auth, config, credential_store, repo_file


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
    # The config dir: a host config.toml (credential_store = "file", say) must
    # not change what a test resolves. An absolute XDG_CONFIG_HOME replaces a
    # host one (a relative one would mean ~) and reaches child processes;
    # macOS platformdirs ignores it, so the patch covers macOS.
    config_home = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(config, "user_config_dir", lambda *a, **kw: str(config_home))
    for name in (credential_store.STORE_ENV, auth.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    auth._LAST_LOOKUP.set(None)
    auth._LAST_WRITE.set(None)
    try:
        yield backend
    finally:
        auth._LAST_LOOKUP.set(None)
        auth._LAST_WRITE.set(None)


CHECKOUT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def outside_this_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests run from this git checkout: never read or write its .solwyn.env.

    A test that needs a repo makes one under tmp_path (a .git directory) and
    chdirs into it; that repo's root is found as usual.
    """
    real = repo_file.git_root

    def guarded(start: Path) -> Path | None:
        root = real(start)
        return None if root is not None and root.resolve() == CHECKOUT else root

    monkeypatch.setattr(repo_file, "git_root", guarded)


@pytest.fixture
def credentials_file() -> Path:
    """The isolated credentials file this test reads and writes."""
    return credential_store.credentials_path()


@pytest.fixture
def keychain_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt in: no keyring backend answers, as on a headless runner."""
    monkeypatch.setattr(credential_store, "backend_factory", fail.Keyring)
