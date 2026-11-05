"""Notification-setting writes run on a Solwyn login only.

A project API key is what an app or agent holds, so it must never change the
alerts that would report it. ``notifications set`` therefore never falls back
to the stored or environment project key: with no login it stops before any
request with exit 3 and a hint to run ``solwyn login``. Reads and delivery
tests keep accepting a key because Core's routes for them do.

These tests use real credential resolution (no patched resolver) so the
credential order itself is what is under test.
"""

import importlib
import json

import httpx
import pytest
from typer.testing import CliRunner

from login_fixtures import LOGIN_TOKEN, login_meta
from solwyn_cli import auth
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app

runner = CliRunner()
KEY = "sk_proj_" + "ab" * 32
CONFIG = {
    "channel_ids": [],
    "channels": [],
    "sms_config": {"enabled": False},
    "email": {"enabled": False},
    "severity_threshold": "warning",
    "enabled_event_types": [],
    "capabilities": {
        "available_channels": ["email"],
        "sms_allowed_events": [],
        "email_allowed_events": ["budget_threshold"],
    },
}


@pytest.fixture
def sent(monkeypatch):
    """Record (method, path, credential) for every request; answer CONFIG."""
    for name in (auth.API_KEY_ENV, auth.AUTH_SELECTION_ENV, auth.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    module = importlib.import_module("solwyn_cli.commands._notifications_common")
    calls = []

    def request(self, method, path, **kwargs):
        calls.append((method, path, self._credential))
        return httpx.Response(200, json=CONFIG)

    monkeypatch.setattr(module.SolwynClient, "request", request)
    return calls


def invoke(*args):
    return runner.invoke(app, ["--project", "proj_one", "--json", *args])


def error_of(result):
    return json.loads(result.stderr)


def test_set_refuses_the_stored_project_key_without_a_request(sent):
    auth.store_project_key(KEY)

    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 3
    error = error_of(result)
    assert error["code"] == "authentication_required"
    assert "project API keys cannot change" in error["message"]
    assert "solwyn login" in error["hint"]
    assert KEY not in result.output
    assert sent == []


def test_set_refuses_an_environment_project_key_without_a_request(sent, monkeypatch):
    monkeypatch.setenv(auth.API_KEY_ENV, KEY)

    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 3
    assert "solwyn login" in error_of(result)["hint"]
    assert sent == []


def test_set_with_no_credential_names_the_login_not_a_key(sent):
    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 3
    error = error_of(result)
    assert error["code"] == "authentication_required"
    assert (
        "Changing notification settings requires a Solwyn login" in (error["message"])
    )
    assert "solwyn login" in error["hint"]
    assert "SOLWYN_API_KEY" not in error["hint"]
    assert sent == []


def test_set_uses_the_login_even_when_a_key_is_also_stored(sent):
    auth.store_project_key(KEY)
    auth.store_login(LOGIN_TOKEN, login_meta())

    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 0, result.output
    [(method, path, credential)] = sent
    assert (method, path) == ("PUT", "/api/v1/projects/proj_one/notifications")
    assert isinstance(credential, auth.LoginCredential)


def test_set_refuses_a_key_forced_by_solwyn_auth_and_says_so(sent, monkeypatch):
    auth.store_project_key(KEY)
    auth.store_login(LOGIN_TOKEN, login_meta())
    monkeypatch.setenv(auth.AUTH_SELECTION_ENV, "project_key")

    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 3
    assert "SOLWYN_AUTH=project_key" in error_of(result)["hint"]
    assert sent == []


def test_set_with_solwyn_auth_login_and_no_login_fails_on_auth(sent, monkeypatch):
    auth.store_project_key(KEY)
    monkeypatch.setenv(auth.AUTH_SELECTION_ENV, "login")

    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 3
    assert "solwyn login" in error_of(result)["hint"]
    assert sent == []


@pytest.mark.parametrize(
    "args,method,path",
    [
        (("notifications", "show"), "GET", "/api/v1/projects/proj_one/notifications"),
        (
            ("notifications", "test", "sms"),
            "POST",
            "/api/v1/projects/proj_one/notifications/test-sms",
        ),
    ],
)
def test_reads_and_sms_test_still_fall_back_to_the_stored_key(
    sent, monkeypatch, args, method, path
):
    # Core's GET and test routes accept a project key, so these keep the
    # login-then-key order; only settings writes became login-only.
    module = importlib.import_module("solwyn_cli.commands._notifications_common")
    monkeypatch.setattr(
        module.SolwynClient,
        "request",
        lambda self, m, p, **_: (
            sent.append((m, p, self._credential))
            or httpx.Response(
                200,
                json=CONFIG if m == "GET" else {"success": True, "message": "eligible"},
            )
        ),
    )
    auth.store_project_key(KEY)

    result = invoke(*args)

    assert result.exit_code == 0, result.output
    [(sent_method, sent_path, credential)] = sent
    assert (sent_method, sent_path) == (method, path)
    assert isinstance(credential, auth.ProjectKeyCredential)


def test_core_api_key_not_allowed_403_is_an_auth_error_with_a_login_hint(
    sent, monkeypatch
):
    # Defense in depth: if Core still sees a key on the write (for example a
    # key placed in SOLWYN_TOKEN), its structured refusal stays actionable.
    module = importlib.import_module("solwyn_cli.commands._notifications_common")

    def refuse(self, method, path, **kwargs):
        sent.append((method, path, self._credential))
        raise error_from_response(
            httpx.Response(
                403,
                json={
                    "detail": {
                        "code": "api_key_not_allowed",
                        "message": "Project API keys cannot make this change.",
                    }
                },
            )
        )

    monkeypatch.setattr(module.SolwynClient, "request", refuse)
    auth.store_login(LOGIN_TOKEN, login_meta())

    result = invoke("notifications", "set", "--no-email")

    assert result.exit_code == 3
    error = error_of(result)
    assert error["code"] == "api_key_not_allowed"
    assert "solwyn login" in error["hint"]
    assert len(sent) == 1


def test_help_states_the_write_is_login_only():
    result = runner.invoke(app, ["notifications", "set", "--help"])

    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "project API keys refused" in text
    assert "full-scope key" not in text
