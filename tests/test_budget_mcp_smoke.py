"""Exercise MCP smoke result checks without staging access."""

import importlib.util
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def smoke(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "budget_mcp_smoke", SCRIPTS / "budget-plane-mcp-smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_denial_is_a_valid_decision_not_a_tool_failure(smoke):
    result = CallToolResult(
        content=[],
        structured_content={
            "schema": "solwyn.mcp.budget-check.v1",
            "ok": True,
            "data": {"allowed": False},
            "error": None,
        },
    )
    assert smoke.unwrap(result, "budget_check")["allowed"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"ok": False},
        {"schema": "wrong"},
        {"data": None},
        {"error": {"message": "private"}},
    ],
)
def test_mcp_rejects_bad_envelopes_without_exposing_response(smoke, change):
    body = {
        "schema": "solwyn.mcp.budget-check.v1",
        "ok": True,
        "data": {"allowed": True},
        "error": None,
    } | change
    result = CallToolResult(content=[], structured_content=body)
    with pytest.raises(smoke.SmokeFailure) as error:
        smoke.unwrap(result, "budget_check")
    assert "private" not in str(error.value)


def test_mcp_is_error_cannot_be_hidden_by_success_payload(smoke):
    result = CallToolResult(
        content=[],
        is_error=True,
        structured_content={
            "schema": "solwyn.mcp.budget-check.v1",
            "ok": True,
            "data": {"allowed": True},
            "error": None,
        },
    )
    with pytest.raises(smoke.SmokeFailure):
        smoke.unwrap(result, "budget_check")


def test_read_profile_probe_launches_real_server_without_resolving_credentials(smoke):
    # Invalid credentials and unreachable API: discovery and excluded-tool
    # rejection must complete without either being used.
    cli = smoke.Cli("http://127.0.0.1:1", "project", "invalid-key", 15)
    anyio.run(smoke.verify_read_profile, cli)


def test_exact_replay_accepts_only_the_documented_legacy_ambiguity(smoke):
    data = {
        "settlement": "not_submitted",
        "recorded": {
            "status": "unknown",
            "ingested": 0,
            "rejected": [],
            "duplicates": [{"index": 0, "call_id": "original", "reason": "legacy_key"}],
        },
    }
    result = CallToolResult(
        content=[],
        is_error=True,
        structured_content={
            "schema": "solwyn.mcp.budget-confirm.v1",
            "ok": False,
            "error": {"code": "recording_unknown"},
            "data": data,
        },
    )
    assert (
        smoke.unwrap(result, "budget_confirm", replay_call_id="original")["settlement"]
        == "not_submitted"
    )
    with pytest.raises(smoke.SmokeFailure):
        smoke.unwrap(result, "budget_confirm")
    with pytest.raises(smoke.SmokeFailure):
        smoke.unwrap(result, "budget_confirm", replay_call_id="different")
