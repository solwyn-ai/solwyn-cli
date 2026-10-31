"""Authentication command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.utils import strip_ansi

from golden.harness import assert_matches_golden
from login_fixtures import LOGIN_TOKEN, login_credential
from solwyn_cli import auth
from solwyn_cli import config as config_module
from solwyn_cli.commands import login as login_module
from solwyn_cli.commands import whoami as whoami_module
from typer.testing import CliRunner

from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "a1" * 32
ACCESS_TOKEN = LOGIN_TOKEN


class FakeClient:
    """Minimal closeable command client with queued request results."""

    instances: list["FakeClient"] = []
    responses: list[httpx.Response | BaseException] = []

    def __init__(self, *, api_url: str, credential: object = None) -> None:
        self.api_url = api_url
        self.credential = credential
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    @property
    def current_credential(self):
        return self.credential

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def _respond(
        self, method: str, path: str, kwargs: dict[str, Any]
    ) -> httpx.Response:
        self.requests.append((method, path, kwargs))
        result = self.__class__.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._respond(method, path, kwargs)

    def request_unauthenticated(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        return self._respond(method, path, kwargs)


@pytest.fixture(autouse=True)
def reset_fake_client() -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    yield


def test_auth_commands_are_registered_without_secret_argv_parameters() -> None:
    root_help = runner.invoke(app, ["--help"])
    login_help = runner.invoke(app, ["login", "--help"])
    root_help_text = strip_ansi(root_help.stdout)
    login_help_text = strip_ansi(login_help.stdout)

    assert root_help.exit_code == 0
    assert "login" in root_help_text
    assert "logout" in root_help_text
    assert "whoami" in root_help_text
    assert login_help.exit_code == 0
    assert "--with-key" in login_help_text
    assert "--password" not in login_help_text
    assert "--api-key" not in login_help_text


@pytest.mark.parametrize(
    "options",
    [
        ["--format", "yaml"],
        ["--json", "--format", "table"],
    ],
)
def test_invalid_global_output_options_are_safe_usage_errors(
    options: list[str],
) -> None:
    result = runner.invoke(app, [*options, "whoami"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid or conflicting output options" in result.stderr
    assert "Traceback" not in result.stderr


def test_invalid_api_url_is_a_safe_usage_error_without_echoing_credentials() -> None:
    unsafe_url = "https://user:synthetic-secret@api.solwyn.ai"

    result = runner.invoke(app, ["--api-url", unsafe_url, "whoami"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid API URL" in result.stderr
    assert unsafe_url not in result.stderr
    assert "synthetic-secret" not in result.stderr
    assert "Traceback" not in result.stderr


def test_remote_command_rejects_invalid_config_without_leaking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    secret_text = 'api_url = "unterminated-sensitive-value'
    path.write_text(secret_text, encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: path)

    result = runner.invoke(app, ["--json", "whoami"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Could not load configuration" in result.stderr
    assert secret_text not in result.stderr
    assert "unterminated-sensitive-value" not in result.stderr
    assert "Traceback" not in result.stderr


def test_piped_project_key_is_validated_stored_and_matches_json_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[str] = []
    monkeypatch.setattr(login_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        login_module,
        "store_project_key",
        lambda value, **_: (
            stored.append(value)
            or auth.ProjectKeyCredential(api_key=value, source="keyring")
        ),
    )

    result = runner.invoke(
        app,
        ["--format", "json", "login", "--with-key"],
        input=f"{VALID_KEY}\n",
    )

    assert result.exit_code == 0
    assert stored == [VALID_KEY]
    assert result.stderr == ""
    assert_matches_golden(result.stdout, GOLDEN_DIR / "login-key.json")
    assert VALID_KEY not in result.stdout


def test_tty_project_key_prompt_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_calls: list[tuple[str, bool, bool]] = []

    def prompt(label: str, *, hide_input: bool = False, err: bool = False) -> str:
        prompt_calls.append((label, hide_input, err))
        return VALID_KEY

    monkeypatch.setattr(login_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(login_module.typer, "prompt", prompt)
    monkeypatch.setattr(
        login_module,
        "store_project_key",
        lambda value, **_: auth.ProjectKeyCredential(api_key=value, source="keyring"),
    )

    result = runner.invoke(app, ["login", "--with-key"])

    assert result.exit_code == 0
    assert prompt_calls == [("Project key", True, True)]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "login-key-table.txt")
    assert VALID_KEY not in result.stdout + result.stderr


def test_invalid_piped_key_fails_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = "malformed-sensitive-value"
    monkeypatch.setattr(login_module, "_stdin_is_tty", lambda: False)

    result = runner.invoke(
        app,
        ["--format", "json", "login", "--with-key"],
        input=f"{invalid}\n",
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_project_key"
    assert invalid not in result.stdout + result.stderr + repr(result.exception)


def test_project_key_storage_failure_is_structured_exit_three_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_store(value: str, **_: object) -> None:
        assert value == VALID_KEY
        raise auth.CredentialStorageError(
            "Could not update credentials in the operating-system keychain."
        )

    monkeypatch.setattr(login_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(login_module, "store_project_key", fail_store)

    result = runner.invoke(
        app,
        ["--json", "login", "--with-key"],
        input=f"{VALID_KEY}\n",
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "credential_storage_failed"
    assert VALID_KEY not in result.stderr + repr(result.exception)


def test_logout_clears_all_credentials_and_matches_both_format_goldens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(auth, "logins_in_every_store", lambda: [])
    monkeypatch.setattr(
        login_module,
        "clear_credentials",
        lambda: calls.append(None) or [auth.Cleared("file", "/test/credentials.json")],
    )

    json_result = runner.invoke(app, ["--json", "logout"])
    table_result = runner.invoke(app, ["logout"])

    assert json_result.exit_code == 0
    assert table_result.exit_code == 0
    assert calls == [None, None]
    assert_matches_golden(json_result.stdout, GOLDEN_DIR / "logout.json")
    assert_matches_golden(table_result.stdout, GOLDEN_DIR / "logout-table.txt")
    assert json_result.stderr == table_result.stderr == ""


def test_logout_clear_failure_is_structured_exit_three_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_secrets = (
        VALID_KEY,
        ACCESS_TOKEN,
    )

    def fail_clear() -> None:
        raise auth.CredentialStorageError(
            "Could not clear credentials from the operating-system keychain."
        )

    monkeypatch.setattr(auth, "logins_in_every_store", lambda: [])
    monkeypatch.setattr(login_module, "clear_credentials", fail_clear)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "credential_storage_failed"
    rendered = result.stderr + repr(result.exception)
    for secret in synthetic_secrets:
        assert secret not in rendered


def test_whoami_project_key_uses_configured_project_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolved the real way, so the golden shows what the CLI prints.
    credential = auth.resolve_credential(
        {"SOLWYN_API_KEY": VALID_KEY}, purpose="metering"
    )
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(whoami_module, "SolwynClient", FakeClient)

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_explicit", "whoami"],
        env={"SOLWYN_PROJECT_ID": "proj_environment"},
    )

    assert result.exit_code == 0
    assert FakeClient.instances == []
    assert result.stderr == ""
    assert_matches_golden(result.stdout, GOLDEN_DIR / "whoami-key.json")
    assert VALID_KEY not in result.stdout


def test_whoami_key_missing_project_has_safe_machine_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="keyring")
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)

    result = runner.invoke(app, ["--json", "whoami"], env={})

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["configured_project"] is None
    assert "--project" in payload["hint"]
    assert payload["auth_source"] == "keyring"


def test_whoami_login_fetches_and_validates_identity_and_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(whoami_module, "SolwynClient", FakeClient)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "user_id": "user_123",
                "email_verified": True,
                "email": "person@example.com",
                "first_name": "Person",
                "marketing_emails_subscribed": True,
                "account_id": "acct_123",
                "display_name": "Person Example",
                "subscription_tier": "team",
                "alert_email_override": None,
            },
        ),
        httpx.Response(
            200,
            json={
                "id": "acct_123",
                "display_name": "Person Example",
                "subscription_tier": "team",
                "alert_email_override": None,
                "seat_limit": 5,
                "seats_used": 3,
                "your_role": "admin",
            },
        ),
        httpx.Response(
            200,
            json=[
                {
                    "id": "proj_alpha",
                    "name": "Alpha",
                    "budget_limit": 100.0,
                    "budget_period": "monthly",
                    "budget_mode": "hard_deny",
                    "created_at": "2026-07-10T12:00:00Z",
                },
                {
                    "id": "proj_beta",
                    "name": "Beta",
                    "budget_limit": 50.0,
                    "budget_period": "weekly",
                    "budget_mode": "alert_only",
                    "created_at": "2026-07-10T12:00:00Z",
                },
            ],
        ),
    ]

    result = runner.invoke(app, ["--format", "json", "whoami"])

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert [(method, path) for method, path, _ in client.requests] == [
        ("GET", "/api/v1/account/me"),
        ("GET", "/api/v1/accounts/me"),
        ("GET", "/api/v1/projects"),
    ]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "whoami-login.json")
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered


def test_whoami_client_auth_failure_is_structured_exit_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(whoami_module, "SolwynClient", FakeClient)
    FakeClient.responses = [auth.AuthError("Authentication failed.")]

    result = runner.invoke(app, ["--json", "whoami"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_failed"
    assert ACCESS_TOKEN not in result.stderr + repr(result.exception)


def test_whoami_login_table_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(whoami_module, "SolwynClient", FakeClient)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "user_id": "user_123",
                "email_verified": True,
                "email": "person@example.com",
                "first_name": "Person",
                "marketing_emails_subscribed": True,
                "account_id": "acct_123",
                "display_name": "Person Example",
                "subscription_tier": "team",
                "alert_email_override": None,
            },
        ),
        httpx.Response(
            200,
            json={
                "id": "acct_123",
                "display_name": "Person Example",
                "subscription_tier": "team",
                "alert_email_override": None,
                "seat_limit": 5,
                "seats_used": 3,
                "your_role": "admin",
            },
        ),
        httpx.Response(
            200,
            json=[
                {
                    "id": "proj_alpha",
                    "name": "Alpha",
                    "budget_limit": 100.0,
                    "budget_period": "monthly",
                    "budget_mode": "hard_deny",
                    "created_at": "2026-07-10T12:00:00Z",
                }
            ],
        ),
    ]

    result = runner.invoke(app, ["--no-color", "whoami"])

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "whoami-login-table.txt")
    assert result.stderr == ""


def test_auth_command_goldens_never_contain_synthetic_secrets() -> None:
    for golden in GOLDEN_DIR.glob("*.json"):
        rendered = golden.read_text(encoding="utf-8")
        assert VALID_KEY not in rendered
        assert ACCESS_TOKEN not in rendered


def test_whoami_table_and_no_color_match_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolved the real way, so the golden shows what the CLI prints.
    credential = auth.resolve_credential(
        {"SOLWYN_API_KEY": VALID_KEY}, purpose="metering"
    )
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)

    result = runner.invoke(app, ["--no-color", "-p", "proj_alpha", "whoami"])

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "whoami-key-table.txt")
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_whoami_production_tier(output_format, monkeypatch):
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(whoami_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(whoami_module, "SolwynClient", FakeClient)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "user_id": "user_123",
                "email_verified": True,
                "email": "person@example.com",
                "display_name": "Person Example",
                "subscription_tier": "production",
                "first_name": "Person",
                "marketing_emails_subscribed": True,
                "account_id": "acct_123",
                "alert_email_override": None,
            },
        ),
        httpx.Response(
            200,
            json={
                "id": "acct_123",
                "display_name": "Person Example",
                "subscription_tier": "production",
                "alert_email_override": None,
                "seat_limit": 2,
                "seats_used": 1,
                "your_role": "owner",
            },
        ),
        httpx.Response(200, json=[]),
    ]
    result = runner.invoke(
        app, [*(["--json"] if output_format == "json" else []), "whoami"]
    )
    assert result.exit_code == 0
    assert "production" in result.stdout
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR
        / f"whoami-production-{output_format}.{'json' if output_format == 'json' else 'txt'}",
    )
