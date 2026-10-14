"""Combined CLI/MCP run-control and denial-recording behavior."""

import json

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.commands import budget
from solwyn_cli.main import app
from solwyn_cli.mcp import server as mcp_server
from test_mcp_conformance import VALID_KEY, _call_tool, _install_http


RUN_ID = "integration-job"
STOPPED = {
    "allowed": False,
    "remaining_budget": 0,
    "mode": "hard_deny",
    "budget_limit": 100,
    "current_usage": 18,
    "denied_by_period": "run_stopped",
    "project_id": "proj_alpha",
    "run_control": {
        "version": "1",
        "action": "terminate",
        "agent_run_id": RUN_ID,
        "reason": "manual_kill",
    },
}


def _check(
    monkeypatch, interface, responses, *, no_record=False, from_environment=False
):
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, remaining = _install_http(
        monkeypatch, responses=responses, credentials=[credential]
    )
    monkeypatch.setattr(budget, "SolwynClient", mcp_server.SolwynClient)
    monkeypatch.setattr(budget, "resolve_credential", lambda: credential)
    monkeypatch.delenv("SOLWYN_TAGS", raising=False)
    monkeypatch.delenv("SOLWYN_AGENT_RUN_ID", raising=False)
    if from_environment:
        monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", RUN_ID)
    if interface == "mcp":
        arguments = {
            "estimated_input_tokens": 21,
            "estimated_output_tokens": 30,
            "model": "gpt-4o",
            "provider": "openai",
            "tags": {"env": "prod"},
            "no_record": no_record,
        }
        if not from_environment:
            arguments["agent_run_id"] = RUN_ID
        result = anyio.run(_call_tool, "budget_check", arguments)
        payload = result.structured_content
    else:
        arguments = [
            "--json",
            "budget",
            "check",
            "--estimated-input-tokens",
            "21",
            "--estimated-output-tokens",
            "30",
            "--model",
            "gpt-4o",
            "--provider",
            "openai",
            "--tag",
            "env=prod",
        ]
        if not from_environment:
            arguments.extend(["--agent-run-id", RUN_ID])
        if no_record:
            arguments.append("--no-record")
        result = CliRunner().invoke(app, arguments)
        payload = json.loads(result.stdout) if result.stdout else None
    return result, payload, requests, clients, remaining


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize("recording", ["ingested", "skipped", "not_recorded", "failed"])
@pytest.mark.parametrize("from_environment", [False, True])
def test_stopped_run_keeps_terminal_outcome_and_exactly_one_receipt(
    monkeypatch, interface, recording, from_environment
):
    responses = [httpx.Response(200, json=STOPPED)]
    if recording != "skipped":
        responses.append(
            httpx.Response(202, json={"ingested": 1})
            if recording == "ingested"
            else httpx.Response(
                503,
                headers={"Retry-After": "1"} if recording == "not_recorded" else {},
                json={"detail": "Account tag registry is busy; retry the batch."}
                if recording == "not_recorded"
                else {"detail": "Proxy unavailable"},
            )
        )
    if recording == "not_recorded":
        responses.append(responses[-1])
    result, payload, requests, clients, remaining = _check(
        monkeypatch,
        interface,
        responses,
        no_record=recording == "skipped",
        from_environment=from_environment,
    )
    if interface == "cli":
        assert result.exit_code == 4, result.output
        data = payload
    else:
        assert payload["ok"] is True
        data = payload["data"]
    assert data["reason"] == "run_terminated"
    assert data["run_control"] == STOPPED["run_control"]
    assert data["recorded"]["status"] == (
        "unknown" if recording == "failed" and interface == "mcp" else recording
    )
    assert data["call_id"] == data["recorded"]["call_ids"][0]
    check = requests[0][2]["json"]
    assert check["run_directive_version"] == "1"
    assert check["agent_run_id"] == RUN_ID
    assert check["tags"] == {"env": "prod"}
    assert [r[:2] for r in requests] == [
        ("POST", "/api/v1/budgets/check"),
        *(
            []
            if recording == "skipped"
            else [("POST", "/api/v1/metadata/ingest")]
            * (2 if recording == "not_recorded" else 1)
        ),
    ]
    if recording != "skipped":
        events = requests[1][2]["json"]
        assert len(events) == 1
        event = events[0]
        assert event["call_id"] == data["call_id"]
        assert event["agent_run_id"] == RUN_ID
        assert event["tags"] == {"env": "prod"}
        assert event["status"] == "budget_denied"
        assert event["deny_source"] == "server"
        assert event["deny_reason"] == event["denied_by_period"] == "run_stopped"
        assert event["input_tokens"] == 21
        assert event["estimated_output_bound"] == 30
        assert event["output_tokens"] == event["latency_ms"] == 0
    assert not remaining
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize(
    "invalid_directive",
    [None, {**STOPPED["run_control"], "agent_run_id": "another-job"}],
)
def test_invalid_run_directive_never_records_a_denial(
    monkeypatch, interface, invalid_directive
):
    response = {**STOPPED, "run_control": invalid_directive}
    result, payload, requests, clients, remaining = _check(
        monkeypatch, interface, [httpx.Response(200, json=response)]
    )
    if interface == "cli":
        assert result.exit_code == 1
    else:
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_budget_check_response"
    assert [r[:2] for r in requests] == [("POST", "/api/v1/budgets/check")]
    assert not remaining
    assert all(client.is_closed for client in clients)
