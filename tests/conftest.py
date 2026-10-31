"""Suite-wide isolation from the host's OS keychain.

In-process tests never touch the host's keyring. Each test starts with an
empty in-memory backend that is reachable, as on a desktop, and with the
keychain-unreachable flag cleared. GitHub's runners have no keyring backend
at all, so without this a real read in one test flipped another test's
login hint. A test that needs the unreachable path asks for
``keychain_unreachable``.
"""

from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from solwyn_cli import auth


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
def _hermetic_keychain() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    auth._KEYCHAIN_UNREACHABLE.set(False)
    try:
        yield backend
    finally:
        auth._KEYCHAIN_UNREACHABLE.set(False)
        keyring.set_keyring(previous)


@pytest.fixture
def keychain_unreachable() -> None:
    """Opt in: no keychain backend answers, as on a headless runner."""
    keyring.set_keyring(fail.Keyring())
