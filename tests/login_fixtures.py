"""Synthetic Solwyn CLI logins for tests."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from solwyn_cli import auth

LOGIN_TOKEN = "solwyn_cli_" + "c1" * 32
OTHER_LOGIN_TOKEN = "solwyn_cli_" + "d2" * 32
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def login_meta(**overrides: Any) -> auth.LoginMetadata:
    """Return synthetic login metadata that expires far in the future."""
    values: dict[str, Any] = {
        "id": "6c1f2a90-0000-4000-8000-000000000001",
        "label": "dev-laptop",
        "scope": "read contain",
        "expires_at": FAR_FUTURE,
        "account_id": "acct_synthetic",
        "account_name": "Acme",
        "email": "ada@example.com",
        "role": "owner",
    }
    values.update(overrides)
    return auth.LoginMetadata(**values)


def login_credential(token: str = LOGIN_TOKEN, **meta: Any) -> auth.LoginCredential:
    """Return a login whose bearer is exactly ``token``.

    Arbitrary token strings are allowed so request assertions can name them;
    the keychain path below always stores a valid ``solwyn_cli_`` token.
    """
    return auth.LoginCredential.model_construct(
        token=SecretStr(token), meta=login_meta(**meta), source="keyring"
    )


def login_entries(token: str = LOGIN_TOKEN, **meta: Any) -> dict[str, str]:
    """Return the keychain entries a stored login occupies."""
    return {
        auth.CLI_CREDENTIAL_ENTRY: token,
        auth.CLI_CREDENTIAL_META_ENTRY: json.dumps(
            login_meta(**meta).model_dump(mode="json")
        ),
    }


def expired_login_entries(token: str = LOGIN_TOKEN) -> dict[str, str]:
    """Return keychain entries for a login that expired a day ago."""
    return login_entries(token, expires_at=datetime.now(UTC) - timedelta(days=1))
