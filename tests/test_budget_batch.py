"""Batch admission preserves order, reservation identity and denial attribution."""

import json
from pathlib import Path

import anyio
import httpx
import pytest
from mcp_batch_fixtures import ID, journal
from test_mcp_conformance import VALID_KEY, _call_tool, _install_http
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.commands import budget
from solwyn_cli.main import app
from solwyn_cli.mcp import server as mcp_server

CHECK = {"model": "gpt-4o", "provider": "openai", "estimated_input_tokens": 21}
ALLOWED = {
    "allowed": True,
    "remaining_budget": 90,
    "reservation_id": "res_1",
    "mode": "hard_deny",
    "budget_limit": 100,
    "current_usage": 10,
    "denied_by_period": None,
    "project_id": "proj_be76331b95dfc399cd776d2f",
    "price_hints": None,
    "run_control": None,
}
DENIED = {
    **ALLOWED,
    "allowed": False,
    "reservation_id": None,
    "denied_by_period": "monthly",
}
UNKNOWN = {
    "result": None,
    "error_code": "unknown_model",
    "error_message": "Model cannot be priced",
}


def item(result):
    return {"result": result, "error_code": None, "error_message": None}


def invoke(
    monkeypatch, interface, checks, responses, *, no_record=False, fmt="json", extra=()
):
    credential = auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, remaining = _install_http(
        monkeypatch, responses=responses, credentials=[credential]
    )
    monkeypatch.setattr(budget, "SolwynClient", mcp_server.SolwynClient)
    monkeypatch.setattr(budget, "resolve_credential", lambda **_: credential)
    monkeypatch.delenv("SOLWYN_TAGS", raising=False)
    monkeypatch.delenv("SOLWYN_AGENT_RUN_ID", raising=False)
    if interface == "mcp":
        # This shared behavior fixture speaks the new journal envelope for valid
        # bounded MCP responses; standalone CLI retains the released legacy API.
        for index, response in enumerate(responses):
            if response.status_code == 200:
                body = response.json()
                if body.get("results") and all(
                    entry.get("result") is not None
                    and entry.get("error_code") is None
                    and entry.get("error_message") is None
                    for entry in body["results"]
                ):
                    responses[index] = httpx.Response(
                        200,
                        json=journal([entry["result"] for entry in body["results"]]),
                    )
        result = anyio.run(
            _call_tool,
            "budget_check_batch",
            {"operation_id": ID, "checks": checks, "no_record": no_record},
        )
        payload = result.structured_content
    else:
        args = ["--format", fmt, "budget", "check", "--batch", "-", *extra]
        if no_record:
            args.append("--no-record")
        result = CliRunner().invoke(app, args, input=json.dumps(checks))
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            result.exception
        )
        payload = json.loads(result.stdout) if fmt == "json" and result.stdout else None
    return result, payload, requests, remaining


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize(
    "tallies", [{}, {"uncounted_calls": 3, "uncounted_tokens": 4200}]
)
def test_batch_preserves_caller_advisory_tallies_and_defaults(
    monkeypatch, interface, tallies
):
    result, _, requests, remaining = invoke(
        monkeypatch,
        interface,
        [{**CHECK, **tallies}],
        [httpx.Response(200, json={"results": [item(ALLOWED)]})],
        no_record=True,
    )
    if interface == "cli":
        assert result.exit_code == 0, result.stderr
    else:
        assert not result.is_error
    assert not remaining
    assert len(requests) == 1
    posted = requests[0][2]["json"]["checks"][0]
    assert posted["uncounted_calls"] == tallies.get("uncounted_calls", 0)
    assert posted["uncounted_tokens"] == tallies.get("uncounted_tokens", 0)


@pytest.mark.parametrize("interface", ["cli"])
def test_batch_mixed_dispositions_keep_order_and_single_denial_receipt(
    monkeypatch, interface
):
    checks = [CHECK, {**CHECK, "model": "unknown"}, {**CHECK, "tags": {"env": "prod"}}]
    result, payload, requests, remaining = invoke(
        monkeypatch,
        interface,
        checks,
        [
            httpx.Response(
                200, json={"results": [item(ALLOWED), UNKNOWN, item(DENIED)]}
            ),
            httpx.Response(202, json={"ingested": 1}),
        ],
    )
    if interface == "cli":
        assert result.exit_code == 4, result.output
        data = payload
        assert data["schema"] == "solwyn.budget.check.batch.v1"
    else:
        assert result.is_error is True
        assert payload["ok"] is False
        assert payload["error"]["code"] == "batch_check_failed"
        data = payload["data"]
    assert data["results"][0]["result"]["reservation_id"] == "res_1"
    if interface == "cli":
        assert data["results"][1] == UNKNOWN
    else:
        assert data["results"][1] == {
            "result": None,
            "error_code": "unknown_model",
            "error_message": "The Solwyn API could not complete this check.",
        }
    assert data["results"][2]["result"]["recorded"]["status"] == "ingested"
    assert len(requests) == 2
    assert requests[0][1] == "/api/v1/budgets/check/batch"
    posted = requests[0][2]["json"]["checks"]
    assert [c["model"] for c in posted] == [c["model"] for c in checks]
    assert all(c["price_hints_version"] == "1" for c in posted)
    event = requests[1][2]["json"][0]
    assert event["input_tokens"] == 21
    assert event["tags"] == {"env": "prod"}
    assert event["call_id"] == data["results"][2]["result"]["call_id"]
    assert event["deny_source"] == "server"
    assert not remaining


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize(
    "checks",
    [
        [],
        [CHECK] * 32 + [{**CHECK, "cost_usd": 1}],
        [CHECK, {**CHECK, "tags": {"": "bad"}}],
        [CHECK, {**CHECK, "fallback_models": ["x"]}],
    ],
)
def test_all_inputs_validate_before_any_network(monkeypatch, interface, checks):
    result, payload, requests, _ = invoke(monkeypatch, interface, checks, [])
    if interface == "cli":
        assert result.exit_code == 2
    else:
        assert result.is_error or payload["ok"] is False
    assert not requests


@pytest.mark.parametrize("interface", ["cli"])
def test_chunks_32_with_common_denial_invocation_id(monkeypatch, interface):
    checks = [{**CHECK, "model": f"model-{i}"} for i in range(33)]
    results = [item(ALLOWED)] * 31 + [item(DENIED)]
    result, payload, requests, remaining = invoke(
        monkeypatch,
        interface,
        checks,
        [
            httpx.Response(200, json={"results": results}),
            httpx.Response(202, json={"ingested": 1}),
            httpx.Response(200, json={"results": [item(DENIED)]}),
            httpx.Response(202, json={"ingested": 1}),
        ],
    )
    assert (result.exit_code == 4) if interface == "cli" else payload["ok"]
    posts = [r[2]["json"] for r in requests if r[1].endswith("/batch")]
    assert [len(p["checks"]) for p in posts] == [32, 1]
    assert [c["model"] for p in posts for c in p["checks"]] == [
        c["model"] for c in checks
    ]
    events = [r[2]["json"][0] for r in requests if r[1].endswith("/ingest")]
    assert events[0]["sdk_instance_id"] == events[1]["sdk_instance_id"]
    assert events[0]["call_id"] != events[1]["call_id"]
    assert not remaining


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize(
    "results",
    [
        [item(DENIED)],
        [item(DENIED), {"result": None}],
        [item(DENIED), {**UNKNOWN, "result": ALLOWED}],
    ],
)
def test_invalid_response_never_ingests_partial_denials(
    monkeypatch, interface, results
):
    result, payload, requests, _ = invoke(
        monkeypatch,
        interface,
        [CHECK, CHECK],
        [httpx.Response(200, json={"results": results})],
    )
    if interface == "cli":
        assert result.exit_code == 1
    else:
        assert payload["ok"] is False
    assert len(requests) == 1


@pytest.mark.parametrize(
    "result_item,exit_code", [(item(ALLOWED), 0), (UNKNOWN, 1), (item(DENIED), 4)]
)
@pytest.mark.parametrize("fmt", ["table", "json"])
def test_batch_output_goldens(monkeypatch, result_item, exit_code, fmt):
    monkeypatch.setattr(budget, "uuid4", lambda: "12345678-1234-1234-1234-123456789abc")
    result, _, _, _ = invoke(
        monkeypatch,
        "cli",
        [CHECK],
        [httpx.Response(200, json={"results": [result_item]})],
        no_record=True,
        fmt=fmt,
    )
    assert result.exit_code == exit_code, result.output
    name = {0: "allowed", 1: "unknown", 4: "denied"}[exit_code]
    assert (
        result.stdout
        == Path(f"tests/golden/budget-check-batch-{name}.{fmt}").read_text()
    )


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize(
    "status,detail,exit_code",
    [
        (404, "Project not found", 5),
        (503, "Budget backend temporarily unavailable; retry", 1),
        (403, {"code": "read_only_key", "message": "Read-only key"}, 3),
    ],
)
def test_whole_request_failures_are_not_item_errors(
    monkeypatch, interface, status, detail, exit_code
):
    result, payload, requests, _ = invoke(
        monkeypatch,
        interface,
        [CHECK],
        [httpx.Response(status, json={"detail": detail})],
    )
    if interface == "cli":
        assert result.exit_code == exit_code
        assert not result.stdout
    else:
        assert payload["ok"] is False
        assert payload["error"]["status_code"] == status
    assert len(requests) == 1


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize("recording", ["ingested", "skipped", "not_recorded", "failed"])
def test_batch_stopped_run_terminal_outcome_and_attribution(
    monkeypatch, interface, recording
):
    directive = {
        "version": "1",
        "action": "terminate",
        "agent_run_id": "job-1",
        "reason": "manual_kill",
    }
    stopped = {**DENIED, "denied_by_period": "run_stopped", "run_control": directive}
    responses = [httpx.Response(200, json={"results": [item(stopped)]})]
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
    result, payload, requests, _ = invoke(
        monkeypatch,
        interface,
        [{**CHECK, "agent_run_id": "job-1"}],
        responses,
        no_record=recording == "skipped",
    )
    data = payload if interface == "cli" else payload["data"]
    if interface == "cli":
        assert result.exit_code == 4
    outcome = data["results"][0]["result"]
    assert outcome["reason"] == "run_terminated"
    assert outcome["run_control"] == directive
    assert outcome["recorded"]["status"] == (
        "unknown" if recording == "failed" and interface == "mcp" else recording
    )
    assert requests[0][2]["json"]["checks"][0]["run_directive_version"] == "1"
    assert len(requests) == (
        1 if recording == "skipped" else 3 if recording == "not_recorded" else 2
    )


@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_bad_later_run_directive_prevents_earlier_receipt(monkeypatch, interface):
    stopped = {**DENIED, "denied_by_period": "run_stopped", "run_control": None}
    result, payload, requests, _ = invoke(
        monkeypatch,
        interface,
        [CHECK, {**CHECK, "agent_run_id": "job-1"}],
        [httpx.Response(200, json={"results": [item(DENIED), item(stopped)]})],
    )
    assert result.exit_code == 1 if interface == "cli" else payload["ok"] is False
    assert len(requests) == 1


@pytest.mark.parametrize(
    "extra", [("--model", "gpt-4o"), ("--agent-run-id", "job"), ("--tag", "env=prod")]
)
def test_batch_rejects_ambiguous_single_check_flags(monkeypatch, extra):
    result, _, requests, _ = invoke(monkeypatch, "cli", [CHECK], [], extra=extra)
    assert result.exit_code == 2
    assert not requests


def test_batch_file_input_and_environment_run_opt_in(monkeypatch, tmp_path):
    source = tmp_path / "checks.json"
    source.write_text(json.dumps([CHECK]))
    credential = auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json={"results": [item(ALLOWED)]})],
        credentials=[credential],
    )
    monkeypatch.setattr(budget, "SolwynClient", mcp_server.SolwynClient)
    monkeypatch.setattr(budget, "resolve_credential", lambda **_: credential)
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "env-run")
    monkeypatch.setenv("SOLWYN_TAGS", "env=prod")
    result = CliRunner().invoke(
        app, ["--json", "budget", "check", "--batch", str(source)]
    )
    assert result.exit_code == 0, result.output
    body = requests[0][2]["json"]["checks"][0]
    assert body["agent_run_id"] == "env-run"
    assert body["run_directive_version"] == "1"
    assert body["tags"] == {"env": "prod"}


@pytest.mark.parametrize("interface", ["cli"])
def test_later_chunk_failure_preserves_completed_reservations(monkeypatch, interface):
    result, payload, requests, _ = invoke(
        monkeypatch,
        interface,
        [CHECK] * 33,
        [
            httpx.Response(200, json={"results": [item(ALLOWED)] * 32}),
            httpx.Response(
                503, json={"detail": "Budget backend temporarily unavailable; retry"}
            ),
        ],
    )
    if interface == "cli":
        assert result.exit_code == 1
        assert "do not replay" in result.stderr.lower()
        assert "input index 32" in result.stderr
        data = payload
    else:
        assert payload["ok"] is False
        assert payload["error"]["status_code"] == 503
        data = payload["data"]
    assert data["failed_chunk_start"] == 32
    assert data["total_checks"] == 33
    assert len(data["results"]) == 32
    assert data["results"][0]["result"]["reservation_id"] == "res_1"
    assert len(requests) == 2


@pytest.mark.parametrize("interface", ["cli"])
def test_batch_retries_only_admission_429_with_identical_body(monkeypatch, interface):
    result, payload, requests, remaining = invoke(
        monkeypatch,
        interface,
        [CHECK],
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"results": [item(ALLOWED)]}),
        ],
    )
    assert result.exit_code == 0 if interface == "cli" else payload["ok"]
    assert len(requests) == 2
    assert requests[0][2]["json"] == requests[1][2]["json"]
    assert not remaining


@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_fail_open_item_keeps_null_reservation_and_never_ingests(
    monkeypatch, interface
):
    result, payload, requests, _ = invoke(
        monkeypatch,
        interface,
        [CHECK],
        [
            httpx.Response(
                200, json={"results": [item({**ALLOWED, "reservation_id": None})]}
            )
        ],
    )
    assert result.exit_code == 0 if interface == "cli" else payload["ok"]
    data = payload if interface == "cli" else payload["data"]
    assert data["results"][0]["result"]["reservation_id"] is None
    assert data["results"][0]["result"]["price_hints"] is None
    assert len(requests) == 1


def test_denial_table_returns_receipt_identity(monkeypatch):
    call_id = "12345678-1234-1234-1234-123456789abc"
    monkeypatch.setattr(budget, "uuid4", lambda: call_id)
    result, _, _, _ = invoke(
        monkeypatch,
        "cli",
        [CHECK],
        [httpx.Response(200, json={"results": [item(DENIED)]})],
        no_record=True,
        fmt="table",
    )
    assert call_id in result.stdout


@pytest.mark.parametrize("scenario", ["mixed", "stopped", "partial"])
@pytest.mark.parametrize("fmt", ["table", "json"])
def test_batch_edge_output_goldens(monkeypatch, scenario, fmt):
    monkeypatch.setattr(budget, "uuid4", lambda: "12345678-1234-1234-1234-123456789abc")
    checks, responses, expected_exit = edge_scenario(scenario)
    result, _, _, _ = invoke(
        monkeypatch, "cli", checks, responses, no_record=True, fmt=fmt
    )
    assert result.exit_code == expected_exit
    assert (
        result.stdout
        == Path(f"tests/golden/budget-check-batch-{scenario}.{fmt}").read_text()
    )


def edge_scenario(scenario):
    if scenario == "partial":
        return (
            [CHECK] * 33,
            [
                httpx.Response(200, json={"results": [item(ALLOWED)] * 32}),
                httpx.Response(
                    503,
                    json={"detail": "Budget backend temporarily unavailable; retry"},
                ),
            ],
            1,
        )
    if scenario == "stopped":
        directive = {
            "version": "1",
            "action": "terminate",
            "agent_run_id": "job-1",
            "reason": "manual_kill",
        }
        return (
            [{**CHECK, "agent_run_id": "job-1"}],
            [
                httpx.Response(
                    200,
                    json={
                        "results": [
                            item(
                                {
                                    **DENIED,
                                    "denied_by_period": "run_stopped",
                                    "run_control": directive,
                                }
                            )
                        ]
                    },
                )
            ],
            4,
        )
    return (
        [CHECK, CHECK, CHECK],
        [httpx.Response(200, json={"results": [item(ALLOWED), UNKNOWN, item(DENIED)]})],
        4,
    )
