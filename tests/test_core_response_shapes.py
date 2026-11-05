"""Complete responses in Core's real shapes parse in the CLI and MCP.

Fixtures here carry EVERY property Core's OpenAPI declares for the component,
so a strict model that forgets a field Core returns fails here first. The
completeness tests pin each fixture to the vendored spec: when Core adds a
property, the fixture (and then the model) must follow.
"""

import copy
import functools
import json
from pathlib import Path

import httpx
import pytest
from test_costs_command import WINDOW
from test_mcp_read_gaps import call
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import costs as costs_module
from solwyn_cli.main import app

runner = CliRunner()
SNAPSHOT = json.loads(Path("src/solwyn_cli/spec/openapi_snapshot.json").read_text())
KEY = auth.ApiKeyCredential(api_key="solwyn_sk_" + "a1" * 32, source="environment")

# One cost event exactly as Core's CostEventResponse serializes it, with the
# failover and trace fields populated.
CORE_COST_EVENT = {
    "id": "evt_0123456789abcdef",
    "project_id": "proj_be76331b95dfc399cd776d2f",
    "agent_run_id": "run_0123456789abcdef0123456789abcdef",
    "provider": "anthropic",
    "model": "claude-sonnet-5-5",
    "modality": "text",
    "provider_region": "us-east-1",
    "service_tier": "standard",
    "input_tokens": 1200,
    "output_tokens": 340,
    "cached_input_tokens": 200,
    "reasoning_tokens": 0,
    "audio_input_tokens": None,
    "audio_output_tokens": None,
    "image_input_tokens": None,
    "image_output_tokens": None,
    "cost": 0.0123,
    "denied_cost": 0.0,
    "deny_source": None,
    "deny_reason": None,
    "estimated_cost": 0.015,
    "is_unpriced": False,
    "is_estimated": False,
    "media": None,
    "tags": {"customer": "example"},
    "status": "success",
    "is_model_fallback": False,
    "is_provider_fallback": True,
    "failover_reason": "primary_error",
    "latency_ms": 812.5,
    "sdk_instance_id": "instance_1",
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "span_id": "00f067aa0ba902b7",
    "trace_sampled": True,
    "trace_url": "https://traces.example/trace/4bf92f3577b34da6a3ce929d0e0e4736",
    "receipt_cost": 0.0,
    "receipt_is_estimated": False,
    "created_at": "2026-10-30T14:00:00Z",
}
# The same event with every nullable field null, as Core sends a call with no
# failover and no trace.
NULL_COST_EVENT = {
    **CORE_COST_EVENT,
    "agent_run_id": None,
    "provider_region": None,
    "service_tier": None,
    "cached_input_tokens": None,
    "reasoning_tokens": None,
    "estimated_cost": None,
    "tags": None,
    "is_provider_fallback": False,
    "failover_reason": None,
    "trace_id": None,
    "span_id": None,
    "trace_sampled": None,
    "trace_url": None,
}
NEW_FIELDS = (
    "is_provider_fallback",
    "failover_reason",
    "trace_id",
    "span_id",
    "trace_sampled",
    "trace_url",
)


def _component(name: str) -> dict:
    return SNAPSHOT["components"]["schemas"][name]


def _page(event: dict) -> dict:
    return {
        "items": [event],
        "next_cursor": None,
        "has_more": False,
        "window": WINDOW,
    }


def test_cost_event_fixture_has_exactly_cores_properties() -> None:
    assert set(CORE_COST_EVENT) == set(_component("CostEventResponse")["properties"])
    assert set(_page(CORE_COST_EVENT)) == set(
        _component("CostEventsResponse")["properties"]
    )


def test_failover_reason_fixture_uses_cores_enum() -> None:
    assert CORE_COST_EVENT["failover_reason"] in _component("FailoverReason")["enum"]


@pytest.mark.parametrize("event", [CORE_COST_EVENT, NULL_COST_EVENT])
def test_cli_cost_events_parse_a_complete_core_page(
    monkeypatch: pytest.MonkeyPatch, event: dict
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page(event))

    monkeypatch.setattr(costs_module, "resolve_credential", lambda **_: KEY)
    monkeypatch.setattr(
        costs_module,
        "SolwynClient",
        functools.partial(SolwynClient, transport=httpx.MockTransport(handle)),
    )

    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs", "events"]
    )

    assert result.exit_code == 0, result.stderr
    item = json.loads(result.stdout)["items"][0]
    assert {field: item[field] for field in NEW_FIELDS} == {
        field: event[field] for field in NEW_FIELDS
    }
    assert (
        requests[-1].url.path
        == "/api/v1/projects/proj_be76331b95dfc399cd776d2f/costs/events"
    )


def test_cli_cost_events_table_parses_a_complete_core_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", lambda **_: KEY)
    monkeypatch.setattr(
        costs_module,
        "SolwynClient",
        functools.partial(
            SolwynClient,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_page(CORE_COST_EVENT))
            ),
        ),
    )

    result = runner.invoke(
        app, ["--no-color", "-p", "proj_be76331b95dfc399cd776d2f", "costs", "events"]
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.split()[:2] == ["Denied", "Deny"]


@pytest.mark.parametrize("event", [CORE_COST_EVENT, NULL_COST_EVENT])
def test_mcp_list_cost_events_parses_a_complete_core_page(
    monkeypatch: pytest.MonkeyPatch, event: dict
) -> None:
    result, requests = call(
        monkeypatch,
        "list_cost_events",
        [httpx.Response(200, json=_page(copy.deepcopy(event)))],
        {"project_id": "proj_be76331b95dfc399cd776d2f"},
    )

    assert not result.is_error, result.structured_content
    item = result.structured_content["data"]["items"][0]
    assert {field: item[field] for field in NEW_FIELDS} == {
        field: event[field] for field in NEW_FIELDS
    }
    assert len(requests) == 1
