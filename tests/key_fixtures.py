"""Synthetic API keys, stored keys and a fake Core for the project resolver."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from solwyn_cli import auth, project_resolver

PROJECT_ID = "proj_" + "8a" * 12
OTHER_PROJECT_ID = "proj_" + "9b" * 12
ACCOUNT_ID = "acct_synthetic"
OTHER_ACCOUNT_ID = "acct_other"
KEY = "solwyn_sk_" + "a1" * 32
OTHER_KEY = "solwyn_sk_" + "b2" * 32
KEY_ID = "6f1d0c9e-0000-4000-8000-000000000001"
OTHER_KEY_ID = "6f1d0c9e-0000-4000-8000-000000000002"


def store_key(
    *,
    project_id: str = PROJECT_ID,
    key: str = KEY,
    key_id: str = KEY_ID,
    account_id: str = ACCOUNT_ID,
    project_name: str = "checkout-bot",
    api_url: str | None = None,
    mode: Any = None,
) -> auth.ApiKeyCredential:
    """Store one key the way keys create --store-key does."""
    return auth.store_api_key(
        project_id=project_id,
        key=key,
        key_id=key_id,
        account_id=account_id,
        project_name=project_name,
        api_url=api_url,
        mode=mode,
    )


def key_item(origin: str, project_id: str = PROJECT_ID) -> str:
    """The keyring username of one stored key."""
    return f"{origin}/api_key/{project_id}"


def key_index(origin: str) -> str:
    """The keyring username of the index of stored project ids."""
    return f"{origin}/api_keys"


def key_document(
    *,
    key: str = KEY,
    key_id: str = KEY_ID,
    account_id: str = ACCOUNT_ID,
    project_name: str = "checkout-bot",
    stored_at: str = "2026-11-20T10:00:00Z",
) -> dict[str, str]:
    """The five stored fields of one key, as the file and keyring keep them."""
    return {
        "key": key,
        "key_id": key_id,
        "account_id": account_id,
        "project_name": project_name,
        "stored_at": stored_at,
    }


def keys_entry(project_id: str = PROJECT_ID, **fields: Any) -> str:
    """The ``api_keys`` entry (JSON) holding one key, for a DictStore."""
    import json

    return json.dumps({project_id: key_document(**fields)})


def stored_keychain(
    origin: str, project_id: str = PROJECT_ID, **fields: Any
) -> dict[tuple[str, str], str]:
    """Keyring items (service, username) -> value for one stored key."""
    import json

    return {
        ("solwyn-cli", key_item(origin, project_id)): json.dumps(
            key_document(**fields)
        ),
        ("solwyn-cli", key_index(origin)): json.dumps([project_id]),
    }


def auth_key_body(
    *,
    project_id: str = PROJECT_ID,
    project_name: str = "checkout-bot",
    account_id: str = ACCOUNT_ID,
    key_id: str = KEY_ID,
    key: str = KEY,
) -> dict[str, Any]:
    """A GET /api/v1/auth/key 200 body (Core spec section 2.5)."""
    return {
        "account_id": account_id,
        "key_id": key_id,
        "key_prefix": key[:26],
        "key_name": project_name,
        "scope": "full",
        "created_via": "cli",
        "expires_at": "2027-11-04T10:12:00Z",
        "rotation_grace_until": None,
        "project_id": project_id,
        "project_name": project_name,
        "budget_mode": "hard_deny",
    }


class FakeCore:
    """Answers the resolver's reads; records each (method, path, headers)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.routes: dict[
            tuple[str, str], Callable[[dict[str, str]], httpx.Response]
        ] = {}

    def on(
        self,
        method: str,
        path: str,
        answer: httpx.Response | Callable[[dict[str, str]], httpx.Response],
    ) -> None:
        self.routes[(method, path)] = (
            answer if callable(answer) else (lambda headers, answer=answer: answer)
        )

    def factory(self, *, api_url: str, credential: object = None) -> "_FakeClient":
        return _FakeClient(self, credential)


class _FakeClient:
    def __init__(self, core: FakeCore, credential: object) -> None:
        self.core = core
        self.credential = credential

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    @property
    def current_credential(self) -> object:
        return self.credential

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        from solwyn_cli.errors import error_from_response

        headers = {"authorization": "Bearer " + self.credential.bearer_value()}  # type: ignore[union-attr]
        project = getattr(self.credential, "project", None)
        if isinstance(self.credential, auth.ApiKeyCredential) and project:
            headers["solwyn-project"] = project
        self.core.calls.append((method, path, headers))
        handler = self.core.routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected request {method} {path}")
        response = handler(headers)
        response.request = httpx.Request(method, "https://api.test" + path)
        if not response.is_success:
            raise error_from_response(response)
        return response


@pytest.fixture
def fake_core(monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    """Route every resolver read to an in-memory Core."""
    core = FakeCore()
    monkeypatch.setattr(project_resolver, "_client_factory", lambda: core.factory)
    return core


def same_credential(sent: object, given: object) -> bool:
    """Whether a client got the given credential (a key arrives bound to its
    project, so it is a copy with the same key, not the same object)."""
    if sent is given:
        return True
    return (
        isinstance(sent, auth.ApiKeyCredential)
        and isinstance(given, auth.ApiKeyCredential)
        and sent.api_key == given.api_key
        and sent.source == given.source
    )
