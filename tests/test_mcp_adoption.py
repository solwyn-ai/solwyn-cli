"""Behavioral contracts for the standalone, installable adoption examples."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import anyio
import httpx
import pytest

from solwyn_cli import auth
from solwyn_cli.mcp import server as mcp_server
from test_mcp_conformance import BUDGET_RESPONSE, VALID_KEY, _install_http


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def example():
    path = ROOT / "examples/mcp/python/client.py"
    assert path.is_file(), "The runnable Python adoption client is missing"
    spec = importlib.util.spec_from_file_location("adoption_client", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def envelope(data, *, ok=True, error=None, schema="solwyn.mcp.budget-check.v1"):
    return {"schema": schema, "ok": ok, "data": data, "error": error}


def test_valid_denial_is_a_normal_decision_and_never_provider_permission(example):
    parsed = example.decode_result(
        False, envelope({"allowed": False, "reservation_id": None})
    )
    assert example.provider_gate(parsed) == "denied"


@pytest.mark.parametrize(
    ("allowed", "reservation", "expected"),
    [(True, None, "unreserved"), (True, "res_synthetic", "reserved")],
)
def test_allowed_decision_keeps_null_reservation_distinct(
    example, allowed, reservation, expected
):
    parsed = example.decode_result(
        False, envelope({"allowed": allowed, "reservation_id": reservation})
    )
    assert example.provider_gate(parsed) == expected


@pytest.mark.parametrize(
    "payload",
    [
        envelope({"allowed": "false", "reservation_id": None}),
        envelope({"allowed": True}),
        envelope(
            {"project_id": "proj_0123456789abcdef01234567"},
            schema="solwyn.mcp.budget-status.v1",
        ),
        envelope(None, ok=False, error={"code": "authentication_required"}),
    ],
)
def test_missing_malformed_read_or_error_result_cannot_open_provider_gate(
    example, payload
):
    with pytest.raises(ValueError):
        example.provider_gate(example.decode_result(not payload["ok"], payload))


@pytest.mark.parametrize(
    ("is_error", "payload"),
    [
        (True, envelope({"allowed": True})),
        (False, envelope(None, ok=False, error={"code": "internal_error"})),
        (False, None),
    ],
)
def test_inconsistent_wire_and_envelope_fail_closed(example, is_error, payload):
    with pytest.raises(ValueError):
        example.decode_result(is_error, payload)


def test_error_text_is_never_selected_for_display(example):
    parsed = example.decode_result(
        True,
        envelope(
            None,
            ok=False,
            error={"code": "PRIVATE_REMOTE_CANARY", "message": "PRIVATE_REMOTE_CANARY"},
        ),
    )
    assert example.safe_error_code(parsed) == "execution_error"


@pytest.mark.parametrize(
    "data",
    [
        {"allowed": False, "reservation_id": "res_synthetic"},
        {"allowed": True, "reservation_id": ""},
        {
            "allowed": True,
            "reservation_id": "res_synthetic",
            "run_control": {"action": "terminate"},
        },
    ],
)
def test_contradictory_or_stopped_decisions_cannot_open_provider_gate(example, data):
    with pytest.raises(ValueError):
        example.provider_gate(example.decode_result(False, envelope(data)))


@pytest.mark.parametrize("reservation", [None, "res_existing"])
@pytest.mark.parametrize(
    "indicator",
    [
        {"denied_by_period": "run_stopped"},
        {"denied_by_period": "monthly"},
        {"denied_by_period": ""},
        {"reason": "run_terminated"},
        {"denied_by_period": {"private": "PRIVATE_PERIOD_CANARY"}},
        {"reason": "PRIVATE_REASON_CANARY"},
    ],
)
def test_denial_or_termination_indicator_rejects_both_success_lanes(
    example, reservation, indicator
):
    data = {"allowed": True, "reservation_id": reservation, **indicator}
    with pytest.raises(ValueError) as error:
        example.provider_gate(example.decode_result(False, envelope(data)))
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize(
    "indicators",
    [
        {},
        {"denied_by_period": None},
        {"reason": None},
        {"denied_by_period": None, "reason": None, "run_control": None},
    ],
)
@pytest.mark.parametrize(
    ("allowed", "reservation", "expected"),
    [
        (False, None, "denied"),
        (True, None, "unreserved"),
        (True, "res_existing", "reserved"),
    ],
)
def test_omitted_and_null_indicators_preserve_valid_decisions(
    example, indicators, allowed, reservation, expected
):
    data = {"allowed": allowed, "reservation_id": reservation, **indicators}
    assert (
        example.provider_gate(example.decode_result(False, envelope(data))) == expected
    )


def test_stopped_run_remains_a_normal_denial(example):
    data = {
        "allowed": False,
        "reservation_id": None,
        "denied_by_period": "run_stopped",
        "reason": "run_terminated",
        "run_control": {"action": "terminate"},
    }
    assert (
        example.provider_gate(example.decode_result(False, envelope(data))) == "denied"
    )


def test_explicit_environment_drops_unrelated_secrets(example, monkeypatch):
    monkeypatch.setenv("SOLWYN_API_KEY", "intended-synthetic-key")
    for name in (
        "OPENAI_API_KEY",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "SOLWYN_TAGS",
        "HTTP_PROXY",
    ):
        monkeypatch.setenv(name, "PRIVATE_CANARY")
    environment = example.server_environment()
    assert environment["SOLWYN_API_KEY"] == "intended-synthetic-key"
    assert "PRIVATE_CANARY" not in environment.values()


@pytest.mark.parametrize("mode", ["modern", "legacy"])
def test_delivered_client_discovers_expanded_server_before_budget_read(
    example, monkeypatch, mode
):
    project = "proj_0123456789abcdef01234567"
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(200, json={**BUDGET_RESPONSE, "project_id": project})
        ],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
        project=project,
    )
    server = mcp_server.create_server(
        launch_options=mcp_server.McpLaunchOptions(profile="read", project_id=project)
    )
    # Substitute only process transport; the delivered client, public SDK,
    # server discovery, argument validation and HTTP operation remain real.
    monkeypatch.setattr(example, "stdio_client", lambda *args, **kwargs: server)
    output, code = anyio.run(
        example.read_budget,
        SimpleNamespace(
            executable="synthetic-server",
            project=project,
            api_url="http://synthetic.test",
            timeout=10,
            mode=mode,
            validation_error=False,
        ),
    )
    assert code == 0, output
    assert output == {
        "status": "ok",
        "protocol": "2025-11-25" if mode == "legacy" else "2026-07-28",
        "tools": 33,
        "project_id": project,
        "remaining_budget": 87.5,
    }
    assert len(requests) == 1
    assert requests[0][:2] == ("GET", f"/api/v1/projects/{project}/budget")


@pytest.mark.parametrize(
    "mutation",
    [
        "old_inventory",
        "missing_budget",
        "missing_new_read",
        "missing_preview",
        "missing_members",
        "missing_invites",
        "extra_read",
        "duplicate",
        "writer",
        "write_annotation",
        "missing_annotation",
        "missing_read_hint",
        "non_boolean_read_hint",
        "continued",
    ],
)
def test_delivered_client_rejects_incomplete_write_or_malformed_catalog(
    example, mutation
):
    tools = [
        item.definition.model_copy(deep=True)
        for item in mcp_server.get_tool_catalog(profile="read")
    ]
    catalog = SimpleNamespace(tools=tools, next_cursor=None)
    if mutation == "old_inventory":
        catalog.tools = tools[:13]
    elif mutation == "missing_budget":
        catalog.tools = [tool for tool in tools if tool.name != "budget_status"]
    elif mutation == "missing_new_read":
        catalog.tools = [tool for tool in tools if tool.name != "plan_catalog"]
    elif mutation == "missing_preview":
        catalog.tools = [tool for tool in tools if tool.name != "plan_preview"]
    elif mutation in {"missing_members", "missing_invites"}:
        missing = (
            "list_team_members"
            if mutation == "missing_members"
            else "list_team_invites"
        )
        catalog.tools = [tool for tool in tools if tool.name != missing]
    elif mutation == "extra_read":
        catalog.tools = [*tools, tools[0].model_copy(update={"name": "unknown_read"})]
    elif mutation == "duplicate":
        tools[-1] = tools[0]
    elif mutation == "writer":
        # Even a falsely read-labelled writer must fail exact-name validation.
        tools[-1].name = "budget_check"
    elif mutation == "write_annotation":
        tools[-1].annotations.read_only_hint = False
    elif mutation == "missing_annotation":
        tools[-1].annotations = None
    elif mutation == "missing_read_hint":
        tools[-1].annotations.read_only_hint = None
    elif mutation == "non_boolean_read_hint":
        tools[-1].annotations.read_only_hint = "true"
    else:
        catalog.next_cursor = "PRIVATE_CURSOR_CANARY"
    with pytest.raises(ValueError, match="Unexpected read catalog") as error:
        example.validate_read_catalog(catalog)
    assert "PRIVATE_" not in str(error.value)


def test_wheel_runner_is_a_delivered_acceptance_command():
    assert (ROOT / "scripts/mcp_wheel_smoke.py").is_file()


def test_registry_draft_validator_is_a_delivered_release_check():
    assert (ROOT / "scripts/validate_mcp_registry.py").is_file()
