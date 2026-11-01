"""Local read refusals are actionable, private, and happen before HTTP."""

import anyio
import pytest

from solwyn_cli import auth
from solwyn_cli.mcp import server
from test_mcp_conformance import _call_tool, _install_http
from login_fixtures import (
    fake_keychain,
    OTHER_LOGIN_TOKEN,
    login_credential,
    login_entries,
)

LOGIN_MODES = [
    ("get_costs", {"account": True}),
    ("list_budget_rules", {"with_last_fired": True}),
    ("list_alert_events", {"account": True}),
    ("get_account_alert", {}),
    ("get_account_phone", {}),
    ("list_alert_activity", {}),
    ("list_open_alert_deliveries", {}),
    ("list_audit_events", {}),
    ("list_projects", {}),
    ("get_overview", {}),
]


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = fake_keychain(monkeypatch)
    return values


@pytest.mark.parametrize("name,args", LOGIN_MODES)
@pytest.mark.parametrize(
    "source", ["environment", "keyring", "absent", "forced_project_key"]
)
def test_login_mode_guidance_with_real_credential_precedence(
    monkeypatch,
    keychain,
    name,
    args,
    source,
):
    monkeypatch.delenv("SOLWYN_API_KEY", raising=False)
    monkeypatch.delenv("SOLWYN_AUTH", raising=False)
    key = "sk_proj_" + "a1" * 32
    if source == "environment":
        monkeypatch.setenv("SOLWYN_API_KEY", key)
    elif source == "keyring":
        keychain[auth.API_KEY_ENTRY] = key
    elif source == "forced_project_key":
        # A stored login is skipped only when SOLWYN_AUTH asks for the key.
        keychain.update(login_entries(OTHER_LOGIN_TOKEN))
        keychain[auth.API_KEY_ENTRY] = key
        monkeypatch.setenv("SOLWYN_AUTH", "project_key")
    resolve = auth.resolve_credential
    calls, _, _ = _install_http(monkeypatch, responses=[], credentials=[])
    monkeypatch.setattr(auth, "resolve_credential", resolve)
    result = anyio.run(_call_tool, name, args)
    assert result.is_error
    error = result.structured_content["error"]
    assert error["code"] == "authentication_required"
    # The fixed login hint, then where the stored login was looked for.
    assert error["hint"].startswith(auth.LOGIN_REQUIRED_HINT)
    if source != "forced_project_key":
        assert error["hint"].endswith("Looked in: file, /test/credentials.json.")
    assert "solwyn login" in error["hint"]
    assert "Configure a project key" not in error["hint"]
    assert calls == []
    assert key not in str(result) and OTHER_LOGIN_TOKEN not in str(result)


def test_project_key_capable_mode_retains_normal_auth_guidance(monkeypatch):
    calls, _, _ = _install_http(monkeypatch, responses=[], credentials=[None])
    result = anyio.run(_call_tool, "get_costs", {})
    hint = result.structured_content["error"]["hint"]
    assert hint.startswith(
        "Run solwyn login, or set SOLWYN_TOKEN (a login) or SOLWYN_API_KEY "
        "(a project key) in the environment. Looked in: file, "
    )
    assert calls == []


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"account": True, "project_id": "private-project"}, "Omit project_id"),
        (
            {"account": True, "tag": ["private-tag=value"]},
            "model, provider, modality, group_by",
        ),
        (
            {"account": True, "provider_fallback": False},
            "model, provider, modality, group_by",
        ),
    ],
)
def test_account_cost_query_has_specific_safe_hint(monkeypatch, args, expected):
    calls, _, _ = _install_http(
        monkeypatch,
        responses=[],
        credentials=[login_credential("synthetic")],
    )
    result = anyio.run(_call_tool, "get_costs", args)
    error = result.structured_content["error"]
    assert error["code"] == "invalid_cost_query"
    assert expected in error["hint"]
    assert "key=value" not in error["hint"]
    assert "private-project" not in str(result) and "private-tag" not in str(result)
    assert calls == []


def test_ingest_uncertainty_keeps_priority_over_login_guidance():
    error = auth.AuthError("private-exception-text")
    error.ingest_outcome_uncertain = True
    result = server._operation_error(
        server._CostsResult,
        error,
        invalid_response_code="invalid_costs_response",
        login_required=True,
    )
    assert result.error.hint == server.INGEST_UNKNOWN_HINT
    assert "private-exception-text" not in str(result)
