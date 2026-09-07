"""In-process MCP conformance tests for the supported tool surface."""

from collections.abc import Callable, Iterator
from copy import deepcopy
import json
from pathlib import Path
from threading import get_ident
from typing import Any

import anyio
import httpx
import pytest
from mcp import MCPError
from mcp import Client

from test_alerts_command import EVENT as ALERT_EVENT, WINDOW as ALERT_WINDOW
from solwyn_cli import auth, runtime
from solwyn_cli.client import SolwynClient as RealSolwynClient
from solwyn_cli.mcp import server as mcp_server
from solwyn_cli.output import OutputFormat


VALID_KEY = "sk_proj_" + "a1" * 32
JWT_ACCESS = "mcp-access-token"
JWT_REFRESH = "mcp-refresh-token"
TOOL_NAMES = [
    "budget_status",
    "budget_check",
    "budget_confirm",
    "get_project_status",
    "get_forecast",
    "get_costs",
    "list_projects",
    "get_cost_total",
    "list_budget_rules",
    "record_call",
    "list_tags",
    "list_tag_values",
    "list_agent_runs",
    "get_agent_run",
    "list_alert_events",
    "budget_check_batch",
    "get_overview",
]

BUDGET_RESPONSE = {
    "project_id": "proj_alpha",
    "budget_limit": 100.0,
    "current_usage": 12.5,
    "remaining_budget": 87.5,
    "utilization_pct": 12.5,
    "budget_mode": "hard_deny",
    "budget_period": "monthly",
    "period_date": "2026-07",
    "outage_overspend_ceiling_usd": 0.0,
    "thresholds": [],
    "sub_period": None,
    "sub_limit": None,
    "constraints": {
        "allowed_modes": ["alert_only", "hard_deny"],
        "allowed_periods": ["daily", "weekly", "monthly"],
        "sub_period_enabled": True,
        "max_thresholds": 50,
    },
}
ROUTING = {
    "will_send": True,
    "email": "alerts@example.test",
    "channels": [{"id": "chan_ops", "name": "#ops"}],
    "source": "project_default",
    "muted": None,
}
# One rule of every type the spec declares, in the spec's union order. Live
# projects configure runaway_run and scoped_budget, so budget_status must read
# them rather than fail closed on the response union.
EVERY_RULE_TYPE_THRESHOLDS = [
    {
        "type": "budget_threshold",
        "rule_key": "budget:80",
        "percent": 80,
        "channel_ids": ["channel-budget"],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "forecast_overrun",
        "rule_key": "forecast:125",
        "spend_threshold_usd": 125.5,
        "period": "month",
        "channel_ids": [],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "runaway_run",
        "mode": "alert_only",
        "rule_key": "runaway:50",
        "spend_threshold_usd": 50.0,
        "call_threshold": 100,
        "window_minutes": 60,
        "channel_ids": ["channel-runaway"],
        "routing": ROUTING,
        "snoozed_until": "2026-07-11T12:00:00Z",
        "paused": False,
    },
    {
        "type": "daily_anomaly",
        "rule_key": "daily:2.5:14",
        "multiplier": 2.5,
        "lookback_days": 14,
        "channel_ids": [],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "unit_cost_anomaly",
        "rule_key": "unit:3:21",
        "multiplier": 3.0,
        "lookback_days": 21,
        "channel_ids": [],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": True,
    },
    {
        "type": "scoped_budget",
        "current_usage": None,
        "match_value": None,
        "rule_key": "scoped:model:gpt-4o",
        "scope": "model",
        "match": "gpt-4o",
        "limit": 25.5,
        "mode": "hard_deny",
        "channel_ids": ["channel-scoped"],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
]
CHECK_RESPONSE = {
    "allowed": True,
    "remaining_budget": 86.5,
    "reservation_id": "res_01JZEXAMPLE",
    "mode": "hard_deny",
    "budget_limit": 100.0,
    "current_usage": 13.5,
    "denied_by_period": None,
    "project_id": "proj_alpha",
    "price_hints": {"openai": 0.01},
}
PROVIDERS_RESPONSE = [
    {
        "provider": "openai",
        "total_calls": 10,
        "success_count": 9,
        "error_count": 1,
        "failover_count": 0,
        "model_failover_count": 0,
        "provider_failover_count": 0,
        "success_rate": 90.0,
        "avg_latency_ms": 125.0,
        "circuit_state": "closed",
        "circuit_source": "inferred",
    }
]
FORECAST_RESPONSE = {
    "current_spend": 12.5,
    "forecast_eom": 75.0,
    "burn_rate_per_day": 2.5,
    "ci_low": 60.0,
    "ci_high": 90.0,
    "period_start": "2026-09-01T00:00:00+00:00",
    "period_end": "2026-09-30T23:59:59.999999+00:00",
    "days_elapsed": 5,
    "days_remaining": 25,
    "insufficient_data": False,
}
COSTS_RESPONSE = {
    "window": {
        "requested_from": "2026-07-09T00:00:00Z",
        "requested_to": "2026-07-16T00:00:00Z",
        "effective_from": "2026-07-09T00:00:00Z",
        "effective_to": "2026-07-16T00:00:00Z",
        "retention_clamped": False,
    },
    "truncated": False,
    "project_id": "proj_alpha",
    "range": "30d",
    "group_by": "provider",
    "groups": [
        {
            "group_key": "openai",
            "modality": None,
            "agent_run_name": None,
            "provider": "openai",
            "total_cost": 1.25,
            "total_input_tokens": 100,
            "total_output_tokens": 20,
            "call_count": 2,
            "unpriced_call_count": 0,
            "local_call_count": 0,
        }
    ],
}
OVERVIEW_RESPONSE = {
    "total_spend": 42.5,
    "total_requests": 5100,
    "project_count": 3,
    "trend_vs_last_period": 0.125,
    "period_start": "2026-09-01",
    "period_end": "2026-09-30",
    "savings": {
        "saved_cost": 4.25,
        "saved_cost_upper": None,
        "saved_input_tokens": 12000,
        "blocked_call_count": 6,
    },
    "quota_exceeded": True,
    "included_requests": 5000,
    "current_requests": 5100,
    "window_start": "2026-09-01T00:00:00Z",
    "window_end": "2026-10-01T00:00:00Z",
}
PROJECTS_RESPONSE = [
    {
        "id": "proj_alpha",
        "name": "Alpha",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "hard_deny",
        "created_at": "2026-07-10T12:00:00Z",
    }
]
RUN_ID = "run_" + "1a" * 16
RUN_RESPONSE = {
    "id": RUN_ID,
    "name": "checkout-agent",
    "parent_run_id": None,
    "project_id": "proj_alpha",
    "started_at": "2026-08-01T10:00:00Z",
    "last_seen_at": "2026-08-07T12:30:00Z",
    "total_calls": 42,
    "total_cost": 12.345678,
    "total_denied_cost": 1.25,
    "total_input_tokens": 12000,
    "total_output_tokens": 3400,
    "velocity_flags": ["future_signal"],
    "stopped_at": None,
    "stopped_by_user_id": None,
    "stoppable": True,
}
RUN_LIST_RESPONSE = {
    "runs": [RUN_RESPONSE],
    "next_cursor": None,
    "has_more": False,
    "window": {
        "requested_from": "2026-08-01T00:00:00Z",
        "requested_to": "2026-08-08T00:00:00Z",
        "effective_from": "2026-08-01T00:00:00Z",
        "effective_to": "2026-08-08T00:00:00Z",
        "retention_clamped": False,
    },
}
MCP_TOOLS_GOLDEN = Path(__file__).parent / "golden" / "mcp-tools.json"


async def _listed_tool_contract() -> list[dict[str, Any]]:
    async with Client(mcp_server.create_server(), cache=None) as session:
        tools = (await session.list_tools()).tools
    return [tool.model_dump(mode="json", by_alias=True) for tool in tools]


def _assert_tool_contract(contract: list[dict[str, Any]]) -> None:
    expected = json.loads(MCP_TOOLS_GOLDEN.read_text(encoding="utf-8"))
    assert contract == expected


def _mutate_description(contract: list[dict[str, Any]]) -> None:
    contract[3]["description"] += " changed"


def _mutate_required(contract: list[dict[str, Any]]) -> None:
    contract[1]["inputSchema"]["required"].remove("estimated_input_tokens")


def _mutate_default(contract: list[dict[str, Any]]) -> None:
    contract[5]["inputSchema"]["properties"]["time_range"]["default"] = "30d"


def _mutate_enum(contract: list[dict[str, Any]]) -> None:
    contract[5]["inputSchema"]["$defs"]["CostRange"]["enum"].append("all")


def _mutate_bound(contract: list[dict[str, Any]]) -> None:
    contract[2]["inputSchema"]["properties"]["input_tokens"]["maximum"] = 99


def _mutate_fallback_provider_bound(contract: list[dict[str, Any]]) -> None:
    provider_array = contract[1]["inputSchema"]["properties"]["fallback_providers"][
        "anyOf"
    ][0]
    provider_array.pop("maxItems")


def _mutate_fallback_model_item_bound(contract: list[dict[str, Any]]) -> None:
    model_array = contract[1]["inputSchema"]["properties"]["fallback_models"]["anyOf"][
        0
    ]
    model_array["items"].pop("maxLength")


def _mutate_nullable(contract: list[dict[str, Any]]) -> None:
    contract[0]["inputSchema"]["properties"]["project_id"]["anyOf"].pop()


def _mutate_additional_properties(contract: list[dict[str, Any]]) -> None:
    contract[6]["inputSchema"]["additionalProperties"] = True


def _mutate_nested_output(contract: list[dict[str, Any]]) -> None:
    contract[3]["outputSchema"]["$defs"]["_ProviderHealth"]["required"].remove(
        "circuit_state"
    )


def _mutate_output_required(contract: list[dict[str, Any]]) -> None:
    contract[0]["outputSchema"]["required"].remove("ok")


def test_list_tools_contract_matches_complete_golden() -> None:
    _assert_tool_contract(anyio.run(_listed_tool_contract))


def test_budget_check_schema_advertises_effective_fallback_bounds() -> None:
    budget_check = anyio.run(_listed_tool_contract)[1]
    properties = budget_check["inputSchema"]["properties"]
    provider_array = properties["fallback_providers"]["anyOf"][0]
    model_array = properties["fallback_models"]["anyOf"][0]

    assert provider_array["type"] == "array"
    assert provider_array["maxItems"] == 8
    assert model_array["type"] == "array"
    assert model_array["maxItems"] == 8
    assert model_array["items"] == {"maxLength": 2048, "type": "string"}
    assert properties["model"]["maxLength"] == 2048
    assert "minLength" not in properties["model"]


@pytest.mark.parametrize(
    "mutation",
    [
        _mutate_description,
        _mutate_required,
        _mutate_default,
        _mutate_enum,
        _mutate_bound,
        _mutate_fallback_provider_bound,
        _mutate_fallback_model_item_bound,
        _mutate_nullable,
        _mutate_additional_properties,
        _mutate_nested_output,
        _mutate_output_required,
    ],
)
def test_complete_tool_contract_guard_rejects_schema_drift(
    mutation: Callable[[list[dict[str, Any]]], None],
) -> None:
    contract = deepcopy(anyio.run(_listed_tool_contract))
    mutation(contract)

    with pytest.raises(AssertionError):
        _assert_tool_contract(contract)


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: list[httpx.Response],
    credentials: list[auth.ResolvedCredential | None],
    project: str | None = "proj_alpha",
) -> tuple[
    list[tuple[str, str, dict[str, Any]]],
    list[RealSolwynClient],
    list[httpx.Response],
]:
    requests: list[tuple[str, str, dict[str, Any]]] = []
    clients: list[RealSolwynClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        requests.append(
            (
                request.method,
                request.url.path,
                {
                    "query": dict(request.url.params),
                    "json": None if not body else json.loads(body),
                    "authorization": request.headers.get("Authorization"),
                    "thread_id": get_ident(),
                },
            )
        )
        response = responses.pop(0)
        response.request = request
        return response

    transport = httpx.MockTransport(handler)
    resolved_credentials = iter(credentials)
    command_runtime = runtime.CommandContext(
        api_url="https://api.example.test",
        project=project,
        output_format=OutputFormat.JSON,
        no_color=True,
        environment_no_color=False,
    )
    monkeypatch.setattr(
        runtime, "resolve_command_context", lambda **kwargs: command_runtime
    )
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda: next(resolved_credentials),
    )

    def client_factory(
        *, api_url: str, credential: auth.ResolvedCredential
    ) -> RealSolwynClient:
        client = RealSolwynClient(
            api_url=api_url,
            credential=credential,
            transport=transport,
            sleeper=lambda _: None,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(mcp_server, "SolwynClient", client_factory)
    return requests, clients, responses


async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    async with Client(mcp_server.create_server(), cache=None) as session:
        return await session.call_tool(name, arguments)


def test_get_overview_requires_jwt_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[],
        credentials=[key],
    )

    result = anyio.run(_call_tool, "get_overview", {})

    assert result.structured_content["error"] == {
        "code": "authentication_required",
        "message": "User login is required to read the account overview.",
        "hint": "Unset SOLWYN_API_KEY and run solwyn login, then retry.",
        "status_code": None,
    }
    assert requests == []
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


def test_get_overview_reads_typed_account_summary_with_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = auth.JwtCredential(access_token=JWT_ACCESS, refresh_token=JWT_REFRESH)
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=OVERVIEW_RESPONSE)],
        credentials=[jwt],
    )

    result = anyio.run(_call_tool, "get_overview", {})

    assert result.structured_content["ok"] is True
    assert result.structured_content["schema"] == "solwyn.mcp.overview.v1"
    assert result.structured_content["data"] == OVERVIEW_RESPONSE
    assert _wire_requests(requests) == [
        ("GET", "/api/v1/overview", {}, None),
    ]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


def test_get_overview_invalid_response_is_fixed_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = auth.JwtCredential(access_token=JWT_ACCESS, refresh_token=JWT_REFRESH)
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json={"total_spend": JWT_ACCESS})],
        credentials=[jwt],
    )

    result = anyio.run(_call_tool, "get_overview", {})

    assert result.structured_content["error"] == {
        "code": "invalid_overview_response",
        "message": "The Solwyn API response was invalid.",
        "hint": None,
        "status_code": None,
    }
    assert JWT_ACCESS not in json.dumps(result.structured_content)
    assert _wire_requests(requests) == [("GET", "/api/v1/overview", {}, None)]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


def _wire_requests(
    requests: list[tuple[str, str, dict[str, Any]]],
) -> list[tuple[str, str, dict[str, str], Any]]:
    return [
        (method, path, metadata["query"], metadata["json"])
        for method, path, metadata in requests
    ]


@pytest.fixture
def mcp_http(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    responses: list[httpx.Response] = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=CHECK_RESPONSE),
        httpx.Response(204),
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=PROVIDERS_RESPONSE),
        httpx.Response(200, json=FORECAST_RESPONSE),
        httpx.Response(200, json=COSTS_RESPONSE),
        httpx.Response(200, json=PROJECTS_RESPONSE),
        httpx.Response(200, json=RUN_LIST_RESPONSE),
        httpx.Response(200, json=RUN_RESPONSE),
        httpx.Response(200, json=OVERVIEW_RESPONSE),
    ]

    project_key = auth.ProjectKeyCredential(
        api_key=VALID_KEY,
        source="environment",
    )
    jwt = auth.JwtCredential(access_token=JWT_ACCESS, refresh_token=JWT_REFRESH)
    requests, clients, response_queue = _install_http(
        monkeypatch,
        responses=responses,
        credentials=[
            project_key,
            project_key,
            project_key,
            project_key,
            project_key,
            project_key,
            jwt,
            project_key,
            project_key,
            jwt,
        ],
    )

    yield requests

    assert response_queue == []
    assert len(clients) == 10
    assert all(client.is_closed for client in clients)


def test_exact_tool_contract_and_mocked_http_conformance(
    mcp_http: list[tuple[str, str, dict[str, Any]]],
) -> None:
    caller_thread = get_ident()
    assert list(mcp_server.TOOL_NAMES) == TOOL_NAMES

    async def exercise_protocol() -> None:
        server = mcp_server.create_server()
        async with Client(server, cache=None) as session:
            capabilities = session.server_capabilities
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == TOOL_NAMES
            assert capabilities.prompts is None
            assert capabilities.resources is None
            with pytest.raises(MCPError):
                await session.list_prompts()
            with pytest.raises(MCPError):
                await session.list_resources()
            assert [
                tool.name for tool in (await session.list_tools()).tools
            ] == TOOL_NAMES
            assert all(tool.description for tool in tools)
            assert all(tool.output_schema is not None for tool in tools)
            assert [set(tool.input_schema["properties"]) for tool in tools] == [
                {"project_id"},
                {
                    "estimated_input_tokens",
                    "model",
                    "provider",
                    "fallback_providers",
                    "fallback_models",
                    "agent_run_id",
                    "image_count",
                    "generation_count",
                    "video_seconds",
                    "audio_seconds",
                    "input_characters",
                    "resolution",
                    "quality",
                    "is_estimated",
                    "tags",
                    "no_record",
                    "estimated_output_tokens",
                },
                {
                    "reservation_id",
                    "call_id",
                    "model",
                    "provider",
                    "is_provider_fallback",
                    "provider_region",
                    "service_tier",
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_creation_5m_tokens",
                    "cache_creation_1h_tokens",
                    "reasoning_tokens",
                    "audio_input_tokens",
                    "audio_output_tokens",
                    "image_input_tokens",
                    "image_output_tokens",
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                    "tool_use_input_tokens",
                    "is_estimated",
                    "latency_ms",
                    "status",
                    "agent_run_id",
                    "parent_run_id",
                    "run_name",
                    "tags",
                    "no_record",
                },
                {"project_id"},
                {"project_id"},
                {
                    "project_id",
                    "time_range",
                    "group_by",
                    "from_",
                    "to",
                    "model",
                    "provider",
                    "modality",
                    "status",
                    "service_tier",
                    "provider_region",
                    "agent_run",
                    "failover_reason",
                    "model_fallback",
                    "provider_fallback",
                    "tag",
                },
                set(),
                {
                    "project_id",
                    "time_range",
                    "from_",
                    "to",
                    "model",
                    "provider",
                    "modality",
                    "status",
                    "service_tier",
                    "provider_region",
                    "agent_run",
                    "failover_reason",
                    "model_fallback",
                    "provider_fallback",
                    "tag",
                },
                {"project_id"},
                {
                    "model",
                    "provider",
                    "call_id",
                    "input_tokens",
                    "output_tokens",
                    "token_details",
                    "media_usage",
                    "latency_ms",
                    "status",
                    "modality",
                    "provider_region",
                    "service_tier",
                    "agent_run_id",
                    "parent_agent_run_id",
                    "agent_run_name",
                    "tags",
                },
                {"project_id"},
                {"key", "project_id", "prefix", "limit", "cursor"},
                {
                    "project_id",
                    "time_range",
                    "from_time",
                    "to_time",
                    "query",
                    "parent",
                    "sort",
                    "limit",
                    "cursor",
                },
                {"run", "project_id"},
                {
                    "project_id",
                    "account",
                    "time_range",
                    "from_",
                    "to",
                    "rule_type",
                    "scope",
                    "failed_only",
                    "cursor",
                    "all_pages",
                },
                {"checks", "no_record", "estimated_output_tokens"},
                set(),
            ]

            calls = [
                await session.call_tool("budget_status", {}),
                await session.call_tool(
                    "budget_check",
                    {
                        "estimated_input_tokens": 1200,
                        "model": "gpt-5",
                        "provider": "openai",
                        "fallback_providers": ["anthropic"],
                        "fallback_models": ["claude-sonnet-4"],
                        "agent_run_id": "raw-run-42",
                    },
                ),
                await session.call_tool(
                    "budget_confirm",
                    {
                        "no_record": True,
                        "reservation_id": "res_01JZEXAMPLE",
                        "call_id": "12345678-1234-1234-1234-123456789abc",
                        "model": "gpt-5",
                        "provider": "openai",
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cached_input_tokens": 3,
                        "cache_creation_5m_tokens": 4,
                        "cache_creation_1h_tokens": 5,
                        "reasoning_tokens": 6,
                        "audio_input_tokens": 7,
                        "audio_output_tokens": 8,
                        "image_input_tokens": 9,
                        "image_output_tokens": 10,
                        "accepted_prediction_tokens": 11,
                        "rejected_prediction_tokens": 12,
                        "tool_use_input_tokens": 13,
                        "is_estimated": True,
                    },
                ),
                await session.call_tool("get_project_status", {}),
                await session.call_tool("get_forecast", {}),
                await session.call_tool(
                    "get_costs", {"time_range": "30d", "group_by": "provider"}
                ),
                await session.call_tool("list_projects", {}),
                await session.call_tool(
                    "list_agent_runs",
                    {"time_range": "30d", "query": "checkout"},
                ),
                await session.call_tool("get_agent_run", {"run": RUN_ID}),
                await session.call_tool("get_overview", {}),
            ]

        assert all(call.is_error is False for call in calls)
        assert all(call.structured_content is not None for call in calls)
        assert [call.structured_content["ok"] for call in calls] == [True] * 10
        assert calls[0].structured_content["data"]["project_id"] == "proj_alpha"
        assert (
            calls[1].structured_content["data"]["reservation_id"] == "res_01JZEXAMPLE"
        )
        assert calls[2].structured_content["data"] == {
            "reservation_id": "res_01JZEXAMPLE",
            "call_id": "12345678-1234-1234-1234-123456789abc",
            "recorded": {
                "status": "skipped",
                "ingested": 0,
                "rejected": [],
                "duplicates": [],
                "call_ids": ["12345678-1234-1234-1234-123456789abc"],
            },
        }
        assert calls[3].structured_content["data"]["providers"] == PROVIDERS_RESPONSE
        assert calls[4].structured_content["data"]["forecast_eom"] == 75.0
        assert calls[5].structured_content["data"]["groups"] == COSTS_RESPONSE["groups"]
        assert calls[6].structured_content["data"]["projects"] == PROJECTS_RESPONSE
        assert calls[7].structured_content["data"]["runs"] == [RUN_RESPONSE]
        assert calls[8].structured_content["data"] == RUN_RESPONSE
        assert calls[9].structured_content["data"] == OVERVIEW_RESPONSE

    anyio.run(exercise_protocol)

    assert _wire_requests(mcp_http) == [
        ("GET", "/api/v1/projects/proj_alpha/budget", {}, None),
        (
            "POST",
            "/api/v1/budgets/check",
            {},
            {
                "price_hints_version": "1",
                "estimated_input_tokens": 1200,
                "model": "gpt-5",
                "provider": "openai",
                "fallback_providers": ["anthropic"],
                "fallback_models": ["claude-sonnet-4"],
                "agent_run_id": "raw-run-42",
                "run_directive_version": "1",
            },
        ),
        (
            "POST",
            "/api/v1/budgets/confirm",
            {},
            {
                "reservation_id": "res_01JZEXAMPLE",
                "model": "gpt-5",
                "provider": "openai",
                "is_provider_fallback": False,
                "call_id": "12345678-1234-1234-1234-123456789abc",
                "token_details": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cached_input_tokens": 3,
                    "cache_creation_5m_tokens": 4,
                    "cache_creation_1h_tokens": 5,
                    "reasoning_tokens": 6,
                    "audio_input_tokens": 7,
                    "audio_output_tokens": 8,
                    "image_input_tokens": 9,
                    "image_output_tokens": 10,
                    "accepted_prediction_tokens": 11,
                    "rejected_prediction_tokens": 12,
                    "tool_use_input_tokens": 13,
                    "is_estimated": True,
                },
            },
        ),
        ("GET", "/api/v1/projects/proj_alpha/budget", {}, None),
        ("GET", "/api/v1/projects/proj_alpha/providers", {}, None),
        (
            "GET",
            "/api/v1/projects/proj_alpha/forecast",
            {"period": "mtd"},
            None,
        ),
        (
            "GET",
            "/api/v1/projects/proj_alpha/costs",
            {"range": "30d", "group_by": "provider"},
            None,
        ),
        ("GET", "/api/v1/projects", {}, None),
        (
            "GET",
            "/api/v1/projects/proj_alpha/agent-runs",
            {"range": "30d", "q": "checkout", "sort": "last_seen_at", "limit": "25"},
            None,
        ),
        (
            "GET",
            f"/api/v1/projects/proj_alpha/agent-runs/{RUN_ID}",
            {},
            None,
        ),
        ("GET", "/api/v1/overview", {}, None),
    ]
    assert {request[2]["authorization"] for request in mcp_http} == {
        f"Bearer {VALID_KEY}",
        f"Bearer {JWT_ACCESS}",
    }
    assert all(request[2]["thread_id"] != caller_thread for request in mcp_http)


def test_budget_descriptions_are_live_views_of_canonical_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = mcp_server.agent_guidance()
    canonical_lines = {
        line.removeprefix("- ")
        for line in canonical.splitlines()
        if line.startswith("- ")
    }

    async def list_descriptions() -> dict[str, str]:
        async with Client(mcp_server.create_server(), cache=None) as session:
            return {
                tool.name: tool.description or ""
                for tool in (await session.list_tools()).tools
            }

    descriptions = anyio.run(list_descriptions)
    polling = next(line for line in canonical_lines if "for polling" in line)
    assert descriptions["budget_status"] == polling
    assert descriptions["budget_check"].startswith("Reserve budget only immediately")
    assert descriptions["budget_confirm"].startswith(
        "Settle only a non-null reservation"
    )
    for fragment in (
        "specific un-instrumented",
        "Never wrap SDK-instrumented",
        "reservation_id=null",
        "repeated unconfirmed checks",
    ):
        line = next(line for line in canonical_lines if fragment in line)
        assert line in descriptions["budget_check"]
        assert line in descriptions["budget_confirm"]

    monkeypatch.setattr(
        mcp_server,
        "agent_guidance",
        lambda: canonical.replace("It is a pure read", "It is a SENTINEL pure read"),
    )
    mutated = anyio.run(list_descriptions)
    assert "SENTINEL" in mutated["budget_status"]


def test_auth_and_client_are_fresh_per_call_without_server_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_key = auth.ProjectKeyCredential(
        api_key="sk_proj_" + "b2" * 32,
        source="environment",
    )
    second_key = auth.ProjectKeyCredential(
        api_key="sk_proj_" + "c3" * 32,
        source="environment",
    )
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(200, json=BUDGET_RESPONSE),
            httpx.Response(200, json=BUDGET_RESPONSE),
        ],
        credentials=[first_key, second_key],
    )

    async def two_calls_one_server() -> list[Any]:
        async with Client(mcp_server.create_server(), cache=None) as session:
            return [
                await session.call_tool("budget_status", {}),
                await session.call_tool("budget_status", {}),
            ]

    results = anyio.run(two_calls_one_server)

    assert all(result.structured_content["ok"] for result in results)
    assert _wire_requests(requests) == [
        ("GET", "/api/v1/projects/proj_alpha/budget", {}, None),
        ("GET", "/api/v1/projects/proj_alpha/budget", {}, None),
    ]
    assert [request[2]["authorization"] for request in requests] == [
        f"Bearer {first_key.bearer_value()}",
        f"Bearer {second_key.bearer_value()}",
    ]
    assert responses == []
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    ("credential", "project", "expected_code", "expected_clients"),
    [
        (None, "proj_alpha", "authentication_required", 0),
        (
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment"),
            None,
            "project_required",
            1,
        ),
    ],
)
def test_local_preconditions_return_structured_errors_without_http(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential | None,
    project: str | None,
    expected_code: str,
    expected_clients: int,
) -> None:
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[],
        credentials=[credential],
        project=project,
    )

    result = anyio.run(_call_tool, "budget_status", {})

    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == expected_code
    assert requests == []
    assert responses == []
    assert len(clients) == expected_clients
    assert all(client.is_closed for client in clients)
    assert VALID_KEY not in str(result)


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_code"),
    [
        (
            "budget_check",
            {"estimated_input_tokens": 1, "model": "m", "provider": "openai"},
            "project_key_required",
        ),
        (
            "budget_confirm",
            {
                "reservation_id": "reservation",
                "call_id": "12345678-1234-1234-1234-123456789abc",
                "model": "m",
                "provider": "openai",
            },
            "project_key_required",
        ),
    ],
)
def test_key_only_tools_reject_jwt_without_http(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
    expected_code: str,
) -> None:
    jwt = auth.JwtCredential(access_token=JWT_ACCESS, refresh_token=JWT_REFRESH)
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[],
        credentials=[jwt],
    )

    result = anyio.run(_call_tool, tool_name, arguments)

    assert result.is_error is True
    assert result.structured_content == {
        "schema": f"solwyn.mcp.{tool_name.replace('_', '-')}.v1",
        "ok": False,
        "data": None,
        "error": {
            "code": expected_code,
            "message": (
                "Budget checks require a project API key."
                if tool_name == "budget_check"
                else "Budget confirmation requires a project API key."
            ),
            "hint": "Set SOLWYN_API_KEY or store a project key with solwyn login --with-key.",
            "status_code": None,
        },
    }
    assert requests == []
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed
    assert JWT_ACCESS not in str(result)
    assert JWT_REFRESH not in str(result)


def test_list_projects_returns_structured_jwt_remediation_under_project_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[],
        credentials=[key],
    )

    result = anyio.run(_call_tool, "list_projects", {})

    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "authentication_required"
    assert "solwyn login" in result.structured_content["error"]["hint"]
    assert "JWT" in result.structured_content["error"]["hint"]
    assert requests == []
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed
    assert VALID_KEY not in str(result)


def test_budget_status_reads_every_threshold_rule_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every declared rule type survives the tool's closed response union."""
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                200,
                json={**BUDGET_RESPONSE, "thresholds": EVERY_RULE_TYPE_THRESHOLDS},
            )
        ],
        credentials=[key],
    )

    result = anyio.run(_call_tool, "budget_status", {})

    assert result.structured_content["ok"] is True
    thresholds = result.structured_content["data"]["thresholds"]
    assert [rule["type"] for rule in thresholds] == [
        "budget_threshold",
        "forecast_overrun",
        "runaway_run",
        "daily_anomaly",
        "unit_cost_anomaly",
        "scoped_budget",
    ]
    assert thresholds[2]["spend_threshold_usd"] == 50.0
    assert thresholds[2]["call_threshold"] == 100
    assert thresholds[2]["window_minutes"] == 60
    assert thresholds[5]["scope"] == "model"
    assert thresholds[5]["match"] == "gpt-4o"
    assert thresholds[5]["limit"] == 25.5
    assert thresholds[5]["mode"] == "hard_deny"
    assert _wire_requests(requests) == [
        ("GET", "/api/v1/projects/proj_alpha/budget", {}, None)
    ]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


def test_fail_open_check_returns_null_reservation_without_hidden_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    fail_open = {**CHECK_RESPONSE, "reservation_id": None}
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=fail_open)],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_check",
        {"estimated_input_tokens": 1, "model": "m", "provider": "openai"},
    )

    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["allowed"] is True
    assert result.structured_content["data"]["reservation_id"] is None
    assert _wire_requests(requests) == [
        (
            "POST",
            "/api/v1/budgets/check",
            {},
            {
                "price_hints_version": "1",
                "estimated_input_tokens": 1,
                "model": "m",
                "provider": "openai",
                "fallback_providers": [],
                "fallback_models": [],
            },
        )
    ]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


def test_costs_preserves_402_semantics_without_echoing_server_or_auth_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    responses = [
        httpx.Response(
            402,
            json={
                "detail": {
                    "code": "upgrade_required",
                    "message": f"secret must not escape: {VALID_KEY}",
                    "cta_path": "/billing",
                }
            },
        )
    ]
    requests, clients, response_queue = _install_http(
        monkeypatch,
        responses=responses,
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "get_costs",
        {"time_range": "7d", "group_by": "model"},
    )

    error = result.structured_content["error"]
    assert error == {
        "code": "upgrade_required",
        "message": "The Solwyn API rejected the request.",
        "hint": "Review upgrade options in the dashboard: /billing",
        "status_code": 402,
    }
    assert _wire_requests(requests) == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/costs",
            {"range": "7d", "group_by": "model"},
            None,
        )
    ]
    assert response_queue == []
    assert len(clients) == 1 and clients[0].is_closed
    assert VALID_KEY not in str(result)


@pytest.mark.parametrize(
    ("detail", "expected_code"),
    [
        (
            {
                "code": VALID_KEY,
                "message": "safe",
                "cta_path": "/settings/billing",
            },
            "api_error",
        ),
        (
            {
                "code": "upgrade_required",
                "message": JWT_ACCESS,
                "cta_path": "/billing",
            },
            "upgrade_required",
        ),
        (
            {
                "code": "upgrade_required",
                "message": "safe",
                "cta_path": f"/settings/{VALID_KEY}",
            },
            "upgrade_required",
        ),
        (
            {
                "code": "upgrade_required",
                "message": "safe",
                "cta_path": "https://attacker.example/billing",
            },
            "upgrade_required",
        ),
        (
            {
                "code": "upgrade_required",
                "message": "safe",
                "cta_path": f"/billing/../{VALID_KEY}",
            },
            "upgrade_required",
        ),
        (
            {
                "code": "upgrade_required",
                "message": "safe",
                "cta_path": f"/billing?token={JWT_ACCESS}",
            },
            "upgrade_required",
        ),
        (
            {
                "code": "upgrade_required",
                "message": "safe",
                "cta_path": "/billing/provider-secret-value",
            },
            "upgrade_required",
        ),
        (
            {
                "code": "upgrade_required",
                "message": "safe",
                "hint": "password-super-secret",
                "status": "provider-secret-value",
                "nested": [JWT_REFRESH, {"deep": VALID_KEY}],
            },
            "upgrade_required",
        ),
        (
            {
                "code": {"nested": VALID_KEY},
                "message": [JWT_ACCESS],
                "cta_path": {"nested": "password-super-secret"},
            },
            "api_error",
        ),
    ],
)
def test_remote_error_fields_never_reflect_untrusted_secret_material(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detail: dict[str, Any],
    expected_code: str,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[httpx.Response(402, json={"detail": detail})],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "get_costs",
        {"time_range": "7d", "group_by": "model"},
    )

    rendered = str(result)
    captured = capsys.readouterr()
    for secret in (
        VALID_KEY,
        JWT_ACCESS,
        JWT_REFRESH,
        "password-super-secret",
        "provider-secret-value",
    ):
        assert secret not in rendered
        assert secret not in captured.out
        assert secret not in captured.err
    assert result.structured_content["error"]["code"] == expected_code
    assert result.structured_content["error"]["status_code"] == 402
    if expected_code == "upgrade_required":
        assert result.structured_content["error"]["hint"] == (
            "Review upgrade options in the dashboard: /billing"
        )
    else:
        assert result.structured_content["error"]["hint"] is None
    assert _wire_requests(requests) == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/costs",
            {"range": "7d", "group_by": "model"},
            None,
        )
    ]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        (
            "budget_check",
            {"estimated_input_tokens": 1200, "model": "gpt-5", "provider": "openai"},
            "/api/v1/budgets/check",
        ),
        (
            "budget_confirm",
            {
                "reservation_id": "res_01JZEXAMPLE",
                "call_id": "12345678-1234-1234-1234-123456789abc",
                "model": "gpt-5",
                "provider": "openai",
            },
            "/api/v1/budgets/confirm",
        ),
    ],
)
def test_read_only_key_403_keeps_its_code_and_points_agents_at_key_scope(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    path: str,
) -> None:
    """The code survives normalization and the hint names scope, not account access."""
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, response_queue = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                403,
                json={
                    "detail": {
                        "code": "read_only_key",
                        "message": f"read-only key; secret must not escape: {VALID_KEY}",
                    }
                },
            )
        ],
        credentials=[key],
    )

    result = anyio.run(_call_tool, tool, arguments)

    error = result.structured_content["error"]
    assert error == {
        "code": "read_only_key",
        "message": "The Solwyn API rejected the request.",
        "hint": (
            "The configured project key is read-only; set SOLWYN_API_KEY to a "
            "write-scoped key, then retry. Confirm a key's scope with "
            "solwyn keys show."
        ),
        "status_code": 403,
    }
    assert VALID_KEY not in str(result)
    assert [method for method, _, _ in requests] == ["POST"]
    assert requests[0][1] == path
    assert response_queue == []


def test_plain_403_still_collapses_to_forbidden_without_a_key_scope_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 that is not about key scope keeps the generic account-access hint."""
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    _install_http(
        monkeypatch,
        responses=[httpx.Response(403, json={"detail": "Forbidden."})],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_check",
        {"estimated_input_tokens": 1200, "model": "gpt-5", "provider": "openai"},
    )

    error = result.structured_content["error"]
    assert error["code"] == "forbidden"
    assert error["status_code"] == 403
    assert error["hint"] == "Check account access for this operation, then retry."
    assert "read-only" not in str(error)


def test_agent_runs_range_gate_preserves_safe_source_explanation_and_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                402,
                json={
                    "detail": {
                        "code": "upgrade_required",
                        "message": f"hostile remote detail {VALID_KEY}",
                        "allowed_values": ["7d", "hostile", "90d"],
                        "cta_path": "/billing",
                    }
                },
            )
        ],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "list_agent_runs",
        {"time_range": "30d"},
    )

    assert result.structured_content["error"] == {
        "code": "upgrade_required",
        "message": "Range '30d' is not available on your tier.",
        "hint": "Allowed values: 7d, 90d. Review upgrade options in the dashboard: /billing",
        "status_code": 402,
    }
    assert VALID_KEY not in str(result)
    assert "hostile" not in str(result)
    assert _wire_requests(requests) == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/agent-runs",
            {"range": "30d", "sort": "last_seen_at", "limit": "25"},
            None,
        )
    ]
    assert responses == []


def test_agent_runs_custom_window_gate_does_not_blame_default_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                402,
                json={
                    "detail": {
                        "code": "upgrade_required",
                        "message": "Custom windows longer than 7 days are not available on your tier",
                        "allowed_values": ["7d"],
                        "cta_path": "/billing",
                    }
                },
            )
        ],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "list_agent_runs",
        {
            "from_time": "2026-07-01T00:00:00Z",
            "to_time": "2026-08-01T00:00:00Z",
        },
    )

    error = result.structured_content["error"]
    assert error == {
        "code": "upgrade_required",
        "message": "Custom windows longer than 7 days are not available on your tier.",
        "hint": "Allowed values: 7d. Review upgrade options in the dashboard: /billing",
        "status_code": 402,
    }
    assert "Range '7d'" not in str(error)
    assert _wire_requests(requests) == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/agent-runs",
            {
                "range": "7d",
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-08-01T00:00:00Z",
                "sort": "last_seen_at",
                "limit": "25",
            },
            None,
        )
    ]
    assert responses == []


@pytest.mark.parametrize(
    ("from_time", "to_time"),
    [
        ("1785542400", "1785628800"),
        ("2026-08-01 00:00:00Z", "2026-08-02T00:00:00Z"),
    ],
)
def test_agent_runs_rejects_non_rfc3339_custom_window_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    from_time: str,
    to_time: str,
) -> None:
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
    )

    result = anyio.run(
        _call_tool,
        "list_agent_runs",
        {"from_time": from_time, "to_time": to_time},
    )

    assert result.structured_content["error"] == {
        "code": "custom_window_invalid",
        "message": "The custom run window is invalid.",
        "hint": (
            "Provide both timezone-aware bounds with to_time later than from_time."
        ),
        "status_code": None,
    }


@pytest.mark.parametrize(
    ("arguments", "expected_run_id"),
    [
        ({}, None),
        ({"agent_run_id": "run_42"}, "run_42"),
    ],
)
def test_budget_check_puts_agent_run_id_on_the_wire_only_when_supplied(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    expected_run_id: str | None,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, response_queue = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=CHECK_RESPONSE)],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_check",
        {
            "estimated_input_tokens": 1200,
            "model": "gpt-5",
            "provider": "openai",
            **arguments,
        },
    )

    assert result.structured_content["ok"] is True
    (_, _, _, body) = _wire_requests(requests)[0]
    if expected_run_id is None:
        assert "agent_run_id" not in body
    else:
        assert body["agent_run_id"] == expected_run_id
    assert response_queue == []


@pytest.mark.parametrize(
    ("arguments", "expected_media"),
    [
        ({}, None),
        (
            {"video_seconds": 8.5, "generation_count": 1},
            {"video_seconds": 8.5, "generation_count": 1, "is_estimated": False},
        ),
        (
            {
                "image_count": 2,
                "resolution": "1024x1024",
                "quality": "hd",
                "is_estimated": True,
            },
            {
                "image_count": 2,
                "resolution": "1024x1024",
                "quality": "hd",
                "is_estimated": True,
            },
        ),
    ],
)
def test_budget_check_puts_estimated_media_on_the_wire_only_when_supplied(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    expected_media: dict[str, Any] | None,
) -> None:
    """A text-only call keeps today's body; a media call carries its quantities."""
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, response_queue = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=CHECK_RESPONSE)],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_check",
        {
            "estimated_input_tokens": 1200,
            "model": "gpt-5",
            "provider": "openai",
            **arguments,
        },
    )

    assert result.structured_content["ok"] is True
    (_, _, _, body) = _wire_requests(requests)[0]
    if expected_media is None:
        assert "estimated_media" not in body
    else:
        assert body["estimated_media"] == expected_media
    assert response_queue == []


def test_budget_check_rejects_an_out_of_contract_media_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Media bounds are advertised and enforced before any HTTP call."""
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, response_queue = _install_http(
        monkeypatch,
        responses=[],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_check",
        {
            "estimated_input_tokens": 1200,
            "model": "gpt-5",
            "provider": "openai",
            "image_count": -1,
        },
    )

    assert result.is_error is True
    assert requests == []
    assert response_queue == []


def test_budget_check_honors_exactly_one_429_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=CHECK_RESPONSE),
        ],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_check",
        {"estimated_input_tokens": 1, "model": "m", "provider": "openai"},
    )

    assert result.structured_content["ok"] is True
    expected_request = (
        "POST",
        "/api/v1/budgets/check",
        {},
        {
            "price_hints_version": "1",
            "estimated_input_tokens": 1,
            "model": "m",
            "provider": "openai",
            "fallback_providers": [],
            "fallback_models": [],
        },
    )
    assert _wire_requests(requests) == [expected_request, expected_request]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed


def test_budget_confirm_429_is_structured_safe_and_never_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"detail": f"rate limited {VALID_KEY}"},
            )
        ],
        credentials=[key],
    )

    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {
            "reservation_id": "reservation",
            "call_id": "12345678-1234-1234-1234-123456789abc",
            "model": "m",
            "provider": "openai",
        },
    )

    assert result.is_error is True
    assert result.structured_content == {
        "schema": "solwyn.mcp.budget-confirm.v1",
        "ok": False,
        "data": None,
        "error": {
            "code": "rate_limited",
            "message": "The Solwyn API rejected the request.",
            "hint": None,
            "status_code": 429,
        },
    }
    assert _wire_requests(requests) == [
        (
            "POST",
            "/api/v1/budgets/confirm",
            {},
            {
                "reservation_id": "reservation",
                "model": "m",
                "provider": "openai",
                "is_provider_fallback": False,
                "call_id": "12345678-1234-1234-1234-123456789abc",
                "token_details": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_creation_5m_tokens": 0,
                    "cache_creation_1h_tokens": 0,
                    "reasoning_tokens": 0,
                    "audio_input_tokens": 0,
                    "audio_output_tokens": 0,
                    "image_input_tokens": 0,
                    "image_output_tokens": 0,
                    "accepted_prediction_tokens": 0,
                    "rejected_prediction_tokens": 0,
                    "tool_use_input_tokens": 0,
                    "is_estimated": False,
                },
            },
        )
    ]
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed
    assert VALID_KEY not in str(result)


def test_shared_client_honors_one_jwt_refresh_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = auth.JwtCredential(access_token=JWT_ACCESS, refresh_token=JWT_REFRESH)
    new_access = "rotated-access-token"
    new_refresh = "rotated-refresh-token"
    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access_token, refresh_token: auth.JwtCredential(
            access_token=access_token,
            refresh_token=refresh_token,
        ),
    )
    requests, clients, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(401, json={"detail": "expired"}),
            httpx.Response(
                200,
                json={
                    "access_token": new_access,
                    "refresh_token": new_refresh,
                    "token_type": "bearer",
                },
            ),
            httpx.Response(200, json=PROJECTS_RESPONSE),
        ],
        credentials=[old],
    )

    result = anyio.run(_call_tool, "list_projects", {})

    assert result.structured_content["ok"] is True
    assert _wire_requests(requests) == [
        ("GET", "/api/v1/projects", {}, None),
        (
            "POST",
            "/api/v1/auth/refresh",
            {},
            {"refresh_token": JWT_REFRESH},
        ),
        ("GET", "/api/v1/projects", {}, None),
    ]
    assert requests[0][2]["authorization"] == f"Bearer {JWT_ACCESS}"
    assert requests[2][2]["authorization"] == f"Bearer {new_access}"
    assert responses == []
    assert len(clients) == 1 and clients[0].is_closed
    rendered = str(result)
    for secret in (JWT_ACCESS, JWT_REFRESH, new_access, new_refresh):
        assert secret not in rendered


@pytest.mark.parametrize("tool", ["get_costs", "get_cost_total"])
def test_cost_tools_forward_complete_filters(monkeypatch, tool):
    payload = deepcopy(COSTS_RESPONSE)
    if tool == "get_cost_total":
        payload = {
            "project_id": "proj_alpha",
            "range": "custom",
            "total_cost": 1.25,
            "total_input_tokens": 100,
            "total_output_tokens": 20,
            "call_count": 2,
            "unpriced_call_count": 0,
            "local_call_count": 0,
            "window": COSTS_RESPONSE["window"],
            "savings": None,
        }
    requests, clients, remaining = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=payload)],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )
    args = {
        "from_": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
        "model": "m",
        "provider": "p",
        "modality": "text",
        "status": "success",
        "service_tier": "flex",
        "provider_region": "region",
        "agent_run": "run_" + "a" * 32,
        "failover_reason": "primary_error",
        "model_fallback": False,
        "provider_fallback": True,
        "tag": ["x=a=b"],
    }
    result = anyio.run(_call_tool, tool, args)
    assert result.structured_content["ok"] is True
    query = requests[0][2]["query"]
    assert "range" not in query
    assert query["agent_run_id"] == "run_" + "a" * 32
    assert query["tag.x"] == "a=b"
    assert query["is_model_fallback"] == "false"
    assert query["is_provider_fallback"] == "true"
    assert query["provider_region"] == "region"
    assert not remaining
    assert clients[0].is_closed


@pytest.mark.parametrize("tool", ["get_costs", "get_cost_total"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"from_": "2026-08-01T00:00:00Z"},
        {"tag": ["private-customer-marker=" + "x" * 257]},
    ],
)
def test_invalid_local_cost_queries_have_safe_actionable_errors(
    monkeypatch, tool, arguments
):
    requests, clients, remaining = _install_http(
        monkeypatch,
        responses=[],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )
    result = anyio.run(_call_tool, tool, arguments)
    error = result.structured_content["error"]
    assert result.structured_content["ok"] is False
    assert error["code"] == "invalid_cost_query"
    assert error["message"] == "The cost query was invalid."
    assert "from_" in error["hint"] and "to" in error["hint"]
    assert "key=value" in error["hint"]
    assert error["status_code"] is None
    assert "private-customer-marker" not in str(result)
    assert not requests and not remaining
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("tool", ["get_costs", "get_cost_total"])
def test_invalid_cost_responses_are_not_mislabeled_as_query_errors(monkeypatch, tool):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json={"invalid": "response"})],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )
    result = anyio.run(_call_tool, tool, {})
    assert result.structured_content["error"]["code"] == "invalid_costs_response"
    assert len(requests) == 1


def test_list_budget_rules_is_read_only_with_project_key(monkeypatch):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                200, json={**BUDGET_RESPONSE, "thresholds": EVERY_RULE_TYPE_THRESHOLDS}
            )
        ],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )
    result = anyio.run(_call_tool, "list_budget_rules", {})
    assert not result.is_error
    assert result.structured_content["schema"] == "solwyn.budget.rules.v1"
    assert len(result.structured_content["data"]["rules"]) == 6
    assert [r[:2] for r in requests] == [("GET", "/api/v1/projects/proj_alpha/budget")]


@pytest.mark.parametrize("status", ["ingested", "deduplicated", "rejected", "failed"])
def test_record_call_preserves_dispositions(monkeypatch, status):
    call_id = "12345678-1234-1234-1234-123456789abc"
    body = {"ingested": 1}
    if status == "rejected":
        body = {
            "ingested": 0,
            "rejected": [
                {
                    "index": 0,
                    "code": "unknown_model",
                    "model": "llama3",
                    "message": "Verbatim guidance.",
                }
            ],
        }
    if status in ("deduplicated", "failed"):
        body = {
            "ingested": 0,
            "duplicates": [
                {
                    "index": 0,
                    "reason": "call_id" if status == "deduplicated" else "legacy_key",
                    "call_id": call_id,
                }
            ],
        }
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, responses = _install_http(
        monkeypatch, responses=[httpx.Response(202, json=body)], credentials=[key]
    )
    result = anyio.run(
        _call_tool,
        "record_call",
        {
            "model": "llama3",
            "provider": "ollama",
            "call_id": call_id,
            "tags": {"env": "prod"},
        },
    )
    assert result.structured_content["data"]["status"] == status
    assert result.is_error is (status in {"rejected", "failed"})
    assert result.structured_content["ok"] is (status not in {"rejected", "failed"})
    assert requests[0][1] == "/api/v1/metadata/ingest"
    event = requests[0][2]["json"][0]
    assert event["call_id"] == call_id and event["tags"] == {"env": "prod"}
    assert "lease_id" not in event and "cost_usd" not in event
    assert responses == []


def test_mcp_confirm_records_same_call_id_by_default(monkeypatch):
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, responses = _install_http(
        monkeypatch,
        responses=[httpx.Response(204), httpx.Response(202, json={"ingested": 1})],
        credentials=[key],
    )
    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {
            "reservation_id": "res",
            "model": "gpt-4o",
            "provider": "openai",
            "call_id": "12345678-1234-1234-1234-123456789abc",
            "tags": {"env": "prod"},
            "agent_run_id": "job",
        },
    )
    assert result.structured_content["ok"]
    assert result.structured_content["data"]["recorded"]["status"] == "ingested"
    assert requests[0][2]["json"]["call_id"] == requests[1][2]["json"][0]["call_id"]
    assert requests[1][2]["json"][0]["agent_run_id"] == "job"
    assert "tags" not in requests[0][2]["json"]
    assert responses == []


def test_mcp_denial_receipt(monkeypatch):
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    denied = {
        **CHECK_RESPONSE,
        "allowed": False,
        "reservation_id": None,
        "denied_by_period": "tag",
    }
    requests, _, responses = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(200, json=denied),
            httpx.Response(202, json={"ingested": 1}),
        ],
        credentials=[key],
    )
    result = anyio.run(
        _call_tool,
        "budget_check",
        {
            "model": "gpt-4o",
            "provider": "openai",
            "estimated_input_tokens": 12,
            "estimated_output_tokens": 30,
            "tags": {"env": "prod"},
        },
    )
    data = result.structured_content["data"]
    assert data["allowed"] is False and data["recorded"]["status"] == "ingested"
    event = requests[1][2]["json"][0]
    assert event["call_id"] == data["call_id"] and event["estimated_output_bound"] == 30
    assert event["status"] == "budget_denied" and event["output_tokens"] == 0
    assert responses == []


@pytest.mark.parametrize(
    "tool,args,body",
    [
        ("list_tags", {}, {"keys": [], "usage": {"keys_used": 0}, "scope": "account"}),
        (
            "list_tag_values",
            {"key": "route /? # =", "prefix": "p"},
            {
                "key": "route /? # =",
                "values": [],
                "next_cursor": None,
                "has_more": False,
            },
        ),
    ],
)
def test_mcp_tag_discovery(monkeypatch, tool, args, body):
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, responses = _install_http(
        monkeypatch, responses=[httpx.Response(200, json=body)], credentials=[key]
    )
    result = anyio.run(_call_tool, tool, args)
    assert result.structured_content["ok"]
    assert requests[0][0] == "GET" and "/tags" in requests[0][1]
    if tool == "list_tag_values":
        assert requests[0][2]["query"] == {
            "key": "route /? # =",
            "q": "p",
            "limit": "25",
        }
    assert responses == []


def test_mcp_confirm_bad_uuid_rejected_before_network(monkeypatch):
    requests, _, _ = _install_http(monkeypatch, responses=[], credentials=[])
    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {
            "reservation_id": "res",
            "call_id": "not-a-uuid",
            "model": "gpt-4o",
            "provider": "openai",
        },
    )
    assert result.is_error
    assert requests == []


def test_mcp_record_details_populate_top_level_counts(monkeypatch):
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(202, json={"ingested": 1})],
        credentials=[key],
    )
    result = anyio.run(
        _call_tool,
        "record_call",
        {
            "model": "llama3",
            "provider": "ollama",
            "token_details": {"input_tokens": 12, "output_tokens": 3},
        },
    )
    assert result.structured_content["ok"]
    assert requests[0][2]["json"][0]["input_tokens"] == 12
    assert requests[0][2]["json"][0]["output_tokens"] == 3


def test_mcp_confirm_ingest_failure_does_not_undo_settlement(monkeypatch):
    key = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(204),
            httpx.Response(503, json={"detail": "temporarily unavailable"}),
        ],
        credentials=[key],
    )
    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {
            "reservation_id": "res",
            "call_id": "12345678-1234-1234-1234-123456789abc",
            "model": "gpt-4o",
            "provider": "openai",
        },
    )
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "recording_failed"
    assert "settlement succeeded" in result.structured_content["error"]["message"]
    assert result.structured_content["data"]["reservation_id"] == "res"
    assert (
        result.structured_content["data"]["call_id"]
        == "12345678-1234-1234-1234-123456789abc"
    )
    assert result.structured_content["data"]["recorded"]["status"] == "failed"
    assert len(requests) == 2


def test_list_alert_events_project_key_and_filters(monkeypatch):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                200,
                json={
                    "events": [ALERT_EVENT],
                    "window": ALERT_WINDOW,
                    "next_cursor": "next",
                },
            )
        ],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )
    result = anyio.run(
        _call_tool,
        "list_alert_events",
        {
            "scope": "tag",
            "rule_type": "scoped_budget",
            "failed_only": True,
            "cursor": "start",
            "time_range": "7d",
        },
    )
    assert not result.is_error
    assert (
        result.structured_content["data"]["events"][0]["delivery"]["status"]
        == "partial"
    )
    assert requests[0][:2] == ("GET", "/api/v1/projects/proj_alpha/alert-events")
    assert requests[0][2]["query"] == {
        "range": "7d",
        "scope": "tag",
        "rule_type": "scoped_budget",
        "cursor": "start",
    }


def test_list_alert_events_bad_query(monkeypatch):
    _install_http(
        monkeypatch,
        responses=[],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )
    result = anyio.run(
        _call_tool, "list_alert_events", {"from_": "2026-09-01T00:00:00Z"}
    )
    assert result.structured_content["error"]["code"] == "invalid_alert_query"
