"""All customer reads share strict models, auth and bounded MCP operation policy."""

import anyio
import httpx
import pytest
from mcp import Client

from solwyn_cli import auth
from solwyn_cli.mcp import server
from test_mcp_conformance import _install_http
from test_read_gaps import catalog
from test_alerts_command import EVENT, WINDOW, ACCOUNT
from test_plan_command import BILLING_STATUS

NEW_READS = {
    "list_cost_events",
    "get_cost_baseline",
    "get_unit_economics",
    "get_cost_drift",
    "list_priced_models",
    "simulate_costs",
    "list_provider_failovers",
    "list_untracked_surfaces",
    "list_audit_events",
    "plan_status",
    "plan_catalog",
    "get_whoami",
    "get_account_phone",
    "list_alert_activity",
    "list_open_alert_deliveries",
    "budget_uncounted",
    "get_account_alert",
}


def test_all_new_reads_in_both_profiles_with_policy():
    for profile in ("read", "guardrails"):
        tools = {
            tool.definition.name: tool
            for tool in server.get_tool_catalog(profile=profile)
        }
        assert NEW_READS <= tools.keys()
        for name in NEW_READS:
            annotations = tools[name].definition.annotations
            assert annotations.read_only_hint and annotations.idempotent_hint
            assert annotations.destructive_hint is False
            assert tools[name].definition.input_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "name,path,payload,args",
    [
        ("plan_catalog", "/api/v1/billing/catalog", catalog(), {}),
        ("plan_status", "/api/v1/billing/status", BILLING_STATUS, {}),
        (
            "budget_uncounted",
            "/api/v1/projects/proj_alpha/budget/uncounted",
            dict(
                project_id="proj_alpha",
                uncounted_calls=0,
                uncounted_tokens=0,
                last_reported_at=None,
            ),
            {},
        ),
        (
            "list_alert_activity",
            "/api/v1/account/alerts/activity",
            dict(events=[EVENT], next_cursor=None, window=WINDOW),
            {"time_range": "7d", "event_type": "fired"},
        ),
        (
            "list_open_alert_deliveries",
            "/api/v1/account/alerts/open-deliveries",
            dict(events=[EVENT], next_cursor=None),
            {},
        ),
        ("get_account_alert", "/api/v1/account/alerts", ACCOUNT, {}),
        ("get_account_phone", "/api/v1/account/phone", dict(state="none"), {}),
        (
            "list_priced_models",
            "/api/v1/projects/proj_alpha/costs/priced-models",
            dict(project_id="proj_alpha", pricing_version="synthetic", models=[]),
            {},
        ),
        (
            "get_cost_baseline",
            "/api/v1/projects/proj_alpha/spend-baseline",
            dict(
                project_id="proj_alpha",
                lookback_days=7,
                days_with_data=0,
                mean_daily_spend=0,
                stdev_daily_spend=0,
                armed=False,
            ),
            {},
        ),
        (
            "list_provider_failovers",
            "/api/v1/projects/proj_alpha/providers/failovers",
            [],
            {},
        ),
        (
            "list_audit_events",
            "/api/v1/account/audit-events",
            dict(events=[], next_cursor=None, has_more=False),
            {},
        ),
    ],
)
def test_new_reads_endpoint_and_strict_errors(monkeypatch, name, path, payload, args):
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda: auth.JwtCredential(access_token="synthetic", refresh_token="synthetic"),
    )
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=payload)],
        credentials=[
            auth.JwtCredential(access_token="synthetic", refresh_token="synthetic")
        ],
    )

    async def exercise():
        async with Client(
            server.create_server(
                launch_options=server.McpLaunchOptions(project_id="proj_alpha")
            ),
            cache=None,
        ) as client:
            result = await client.call_tool(name, args)
            assert not result.is_error, result
            assert result.structured_content["ok"] is True

    anyio.run(exercise)
    assert len(requests) == 1
    assert requests[0][:2] == ("GET", path)


def call(monkeypatch, name, payloads, arguments=None, credential=None):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=payloads,
        credentials=[
            credential
            or auth.JwtCredential(access_token="synthetic", refresh_token="synthetic")
        ],
    )

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(exercise), requests


@pytest.mark.parametrize(
    "name,payload",
    [
        ("list_alert_activity", dict(events=[], next_cursor="next", window=WINDOW)),
        ("list_open_alert_deliveries", dict(events=[], next_cursor="next")),
        ("list_audit_events", dict(events=[], next_cursor="next", has_more=True)),
        (
            "list_cost_events",
            dict(items=[], next_cursor="next", has_more=True, window=WINDOW),
        ),
    ],
)
def test_empty_pages_hit_cap_with_exact_unread_cursor(monkeypatch, name, payload):
    from solwyn_cli import _mcp_operation as operation

    monkeypatch.setattr(operation, "MAX_PAGES", 2)
    second = dict(payload, next_cursor="unread")
    result, requests = call(
        monkeypatch,
        name,
        [httpx.Response(200, json=payload), httpx.Response(200, json=second)],
        {"all_pages": True},
    )
    assert result.is_error
    envelope = result.structured_content
    assert envelope["error"]["code"] == "page_limit"
    assert envelope["recovery"]["cursor"] == "unread"
    assert envelope["data"]["next_cursor"] == "unread"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "name,payload",
    [
        ("list_alert_activity", dict(events=[], next_cursor="next", window=WINDOW)),
        ("list_open_alert_deliveries", dict(events=[], next_cursor="next")),
        ("list_audit_events", dict(events=[], next_cursor="next", has_more=True)),
        (
            "list_cost_events",
            dict(items=[], next_cursor="next", has_more=True, window=WINDOW),
        ),
    ],
)
def test_later_page_503_retains_truthful_continuation(monkeypatch, name, payload):
    result, requests = call(
        monkeypatch,
        name,
        [
            httpx.Response(200, json=payload),
            httpx.Response(503, json={"detail": "Unavailable"}),
        ],
        {"all_pages": True},
    )
    assert result.is_error
    envelope = result.structured_content
    assert envelope["error"]["code"] == "service_busy"
    assert envelope["data"]["next_cursor"] == envelope["recovery"]["cursor"] == "next"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "name",
    [
        "list_alert_activity",
        "list_open_alert_deliveries",
        "get_account_alert",
        "get_account_phone",
        "list_audit_events",
    ],
)
def test_jwt_reads_key_gate_and_privacy(monkeypatch, name):
    key = auth.ProjectKeyCredential(
        api_key="sk_proj_" + "a1" * 32, source="environment"
    )
    result, requests = call(monkeypatch, name, [], credential=key)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "authentication_required"
    assert requests == []
    assert key.api_key.get_secret_value() not in str(result)


def test_key_identity_is_local_without_invented_user(monkeypatch):
    key = auth.ProjectKeyCredential(
        api_key="sk_proj_" + "a1" * 32, source="environment"
    )
    result, requests = call(monkeypatch, "get_whoami", [], credential=key)
    assert not result.is_error
    assert result.structured_content["data"]["user"] is None
    assert result.structured_content["data"]["auth_type"] == "project_key"
    assert requests == []
    assert key.api_key.get_secret_value() not in str(result)


def test_uncounted_503_stays_unknown_service_busy(monkeypatch):
    result, requests = call(
        monkeypatch,
        "budget_uncounted",
        [httpx.Response(503, json={"detail": "Unavailable"})],
    )
    assert result.is_error
    assert result.structured_content["data"] is None
    assert result.structured_content["error"]["code"] == "service_busy"
    assert "retry" in result.structured_content["error"]["hint"].lower()
    assert "reservation" not in result.structured_content["error"]["hint"].lower()
    assert len(requests) == 1


@pytest.mark.parametrize("name", sorted(NEW_READS))
def test_unknown_input_is_rejected_before_auth(monkeypatch, name):
    def forbidden():
        raise AssertionError("Invalid arguments reached auth")

    monkeypatch.setattr(auth, "resolve_credential", forbidden)

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            return await client.call_tool(name, {"secret_unknown": "private-input"})

    result = anyio.run(exercise)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_arguments"
    assert "private-input" not in str(result)


def test_account_costs_mutually_exclusive_and_key_gate(monkeypatch):
    result, requests = call(
        monkeypatch, "get_costs", [], {"account": True, "project_id": "explicit"}
    )
    assert (
        result.is_error
        and result.structured_content["error"]["code"] == "invalid_cost_query"
    )
    assert requests == []
    result, requests = call(
        monkeypatch,
        "get_costs",
        [],
        {"account": True},
        auth.ProjectKeyCredential(api_key="sk_proj_" + "a1" * 32, source="environment"),
    )
    assert (
        result.is_error
        and result.structured_content["error"]["code"] == "authentication_required"
    )
    assert requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "success"),
        ("tag", []),
        ("provider_fallback", False),
        ("service_tier", "default"),
    ],
)
def test_account_costs_unsupported_filters_local(monkeypatch, field, value):
    result, requests = call(
        monkeypatch, "get_costs", [], {"account": True, field: value}
    )
    assert (
        result.is_error
        and result.structured_content["error"]["code"] == "invalid_cost_query"
    )
    assert requests == []


def test_simulation_503_never_automatically_retries(monkeypatch):
    result, requests = call(
        monkeypatch,
        "simulate_costs",
        [
            httpx.Response(
                503, headers={"Retry-After": "0"}, json={"detail": "Unavailable"}
            )
        ],
        {"provider": "openai", "model": "synthetic-model"},
    )
    assert result.is_error
    assert len(requests) == 1
    assert requests[0][0] == "POST"


def test_diagnostic_ipc_supports_complete_catalog_and_rejects_overflow():
    from pydantic import ValidationError
    from solwyn_cli._mcp_doctor_ipc import PrivateRequest, HelperResult

    names = [tool.definition.name for tool in server.get_tool_catalog()]
    request = PrivateRequest(
        command="/synthetic/solwyn",
        api_url="https://api.example.invalid",
        profile="guardrails",
        expected_tools=names,
        expected_version="synthetic",
    )
    assert len(request.expected_tools) == 34
    assert len(HelperResult(tools=names).tools) == 34
    with pytest.raises(ValidationError):
        HelperResult(tools=names + ["overflow"])


@pytest.mark.parametrize(
    "name,path,payload,args",
    [
        (
            "get_unit_economics",
            "/api/v1/projects/proj_alpha/unit-economics",
            __import__("test_cost_advice_command").UNIT_ECONOMICS,
            {"vs_prev": False, "cause": True},
        ),
        (
            "get_cost_drift",
            "/api/v1/projects/proj_alpha/costs/drift",
            __import__("test_cost_advice_command").DRIFT,
            {"from_": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"},
        ),
        (
            "simulate_costs",
            "/api/v1/projects/proj_alpha/costs/simulate",
            __import__("test_cost_advice_command").SIMULATION,
            {
                "provider": "bedrock",
                "model": "amazon.nova-pro-v1:0",
                "region": "us-east-1",
                "service_tier": "standard",
            },
        ),
        (
            "list_cost_events",
            "/api/v1/projects/proj_alpha/costs/events",
            dict(items=[], next_cursor=None, has_more=False, window=WINDOW),
            {
                "tag": ["team="],
                "provider_fallback": False,
                "agent_run": "run_" + "1" * 32,
            },
        ),
    ],
)
def test_remaining_cost_reads_exact_queries_body(
    monkeypatch, name, path, payload, args
):
    result, requests = call(
        monkeypatch, name, [httpx.Response(200, json=payload)], args
    )
    assert not result.is_error, result
    assert requests[0][1] == path
    query = requests[0][2]["query"]
    if name == "get_unit_economics":
        assert query == {"range": "30d", "vs_prev": "false", "cause": "true"}
    elif name == "get_cost_drift":
        assert query == {"from": args["from_"], "to": args["to"]}
    elif name == "simulate_costs":
        assert requests[0][0] == "POST"
        assert requests[0][2]["json"] == {
            "target_provider": args["provider"],
            "target_model": args["model"],
            "target_region": args["region"],
            "target_service_tier": args["service_tier"],
        }
    else:
        assert query["tag.team"] == ""
        assert query["is_provider_fallback"] == "false"
        assert query["agent_run_id"] == "run_" + "1" * 32
        assert query["limit"] == "50"


def test_untracked_read_includes_retention_metadata(monkeypatch):
    result, requests = call(
        monkeypatch,
        "list_untracked_surfaces",
        [httpx.Response(200, json=BILLING_STATUS), httpx.Response(200, json=[])],
        {"time_range": "90d"},
    )
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["effective_window_days"] == 90
    assert data["data_retention_days"] == 180
    assert requests[-1][1] == "/api/v1/projects/proj_alpha/untracked-surfaces"
    assert requests[-1][2]["query"] == {"range": "90d"}


def test_account_costs_success_filters_and_authoritative_permissions(monkeypatch):
    from test_costs_command import ACCOUNT_RESPONSE

    result, requests = call(
        monkeypatch,
        "get_costs",
        [httpx.Response(200, json=ACCOUNT_RESPONSE)],
        {
            "account": True,
            "model": "synthetic-model",
            "provider": "openai",
            "modality": "text",
            "from_": "2026-09-01T00:00:00Z",
            "to": "2026-09-02T00:00:00Z",
        },
    )
    assert not result.is_error
    assert result.structured_content["data"]["group_by"] == "project"
    assert requests[0][1] == "/api/v1/account/costs"
    assert requests[0][2]["query"] == {
        "group_by": "project",
        "model": "synthetic-model",
        "provider": "openai",
        "modality": "text",
        "from": "2026-09-01T00:00:00Z",
        "to": "2026-09-02T00:00:00Z",
    }
    for status in (402, 403):
        result, requests = call(
            monkeypatch,
            "get_costs",
            [httpx.Response(status, json={"detail": "PRIVATE_REMOTE"})],
            {"account": True},
        )
        assert result.is_error
        assert result.structured_content["error"]["status_code"] == status
        assert "PRIVATE_REMOTE" not in str(result)


def test_account_latest_read_only_post_and_threshold_rule_mapping(monkeypatch):
    from test_budget_set_command import _response

    fire = dict(
        EVENT,
        event_type="fired",
        rule_type="budget_threshold",
        rule_key="old-key",
        metadata_json={"percent": 80},
    )
    result, requests = call(
        monkeypatch,
        "list_budget_rules",
        [
            httpx.Response(200, json=_response()),
            httpx.Response(200, json=dict(events=[fire], window=WINDOW)),
        ],
        {"with_last_fired": True},
    )
    assert not result.is_error, result
    assert (
        result.structured_content["data"]["last_fired"]["budget:80"]["id"] == fire["id"]
    )
    assert result.structured_content["data"]["last_fired"]["forecast:125"] is None
    assert requests[1][:2] == ("POST", "/api/v1/account/alerts/latest-events")
    assert requests[1][2]["json"]["rules"][0]["percent"] == 80


@pytest.mark.parametrize("name", sorted(NEW_READS))
def test_new_reads_503_or_invalid_response_private(monkeypatch, name):
    args = (
        {"provider": "openai", "model": "synthetic-model"}
        if name == "simulate_costs"
        else {}
    )
    responses = [httpx.Response(503, json={"detail": "PRIVATE_REMOTE"})]
    if name == "list_untracked_surfaces":
        responses.append(httpx.Response(503, json={"detail": "PRIVATE_REMOTE"}))
    result, requests = call(monkeypatch, name, responses, args)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "service_busy"
    assert result.structured_content["data"] is None
    assert "PRIVATE_REMOTE" not in str(result)
    responses = [
        httpx.Response(200, json={"unexpected_private_field": "PRIVATE_REMOTE"})
    ]
    if name == "list_untracked_surfaces":
        responses.insert(0, httpx.Response(200, json=BILLING_STATUS))
    result, requests = call(monkeypatch, name, responses, args)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_read_response"
    assert "PRIVATE_REMOTE" not in str(result)


@pytest.mark.parametrize(
    "name",
    [
        "list_alert_activity",
        "list_open_alert_deliveries",
        "list_audit_events",
        "list_cost_events",
    ],
)
def test_native_stream_size_cap_preserves_first_unread_page(monkeypatch, name):
    from contextlib import asynccontextmanager
    from solwyn_cli import _mcp_operation as operation
    from test_mcp_operation import Body

    calls = []
    page = (
        dict(items=[], has_more=True, next_cursor="unread", window=WINDOW)
        if name == "list_cost_events"
        else dict(events=[], next_cursor="unread")
    )
    if name == "list_alert_activity":
        page["window"] = WINDOW
    if name == "list_audit_events":
        page["has_more"] = True
    first = httpx.Response(200, json=page)
    monkeypatch.setattr(operation, "MAX_RESPONSE_BYTES", len(first.content) + 8)
    body = Body([b"123456789"])

    async def handler(request):
        calls.append(request)
        return first if len(calls) == 1 else httpx.Response(200, stream=body)

    original = operation.operation_scope

    @asynccontextmanager
    async def scope():
        async with original(transport=httpx.MockTransport(handler)) as control:
            yield control

    monkeypatch.setattr(operation, "operation_scope", scope)
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda: auth.JwtCredential(access_token="synthetic", refresh_token="synthetic"),
    )

    async def exercise():
        async with Client(
            server.create_server(
                launch_options=server.McpLaunchOptions(project_id="proj_alpha")
            ),
            cache=None,
        ) as client:
            return await client.call_tool(name, {"all_pages": True})

    result = anyio.run(exercise)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "response_size_limit"
    assert (
        result.structured_content["data"]["next_cursor"]
        == result.structured_content["recovery"]["cursor"]
        == "unread"
    )
    assert len(calls) == 2 and body.closed


def test_native_deadline_applies_to_new_read(monkeypatch):
    from contextlib import asynccontextmanager
    from solwyn_cli import _mcp_operation as operation

    calls = []

    async def handler(request):
        calls.append(request)
        await anyio.sleep(10)
        return httpx.Response(200, json=catalog())

    original = operation.operation_scope

    @asynccontextmanager
    async def scope():
        async with original(transport=httpx.MockTransport(handler)) as control:
            yield control

    monkeypatch.setattr(operation, "operation_scope", scope)
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda: auth.JwtCredential(access_token="synthetic", refresh_token="synthetic"),
    )

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            return await client.call_tool("plan_catalog", {})

    result = anyio.run(exercise)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "operation_deadline"
    assert result.structured_content["data"] is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name,payload",
    [
        ("list_alert_activity", dict(events=[], next_cursor="repeat", window=WINDOW)),
        ("list_open_alert_deliveries", dict(events=[], next_cursor="repeat")),
        ("list_audit_events", dict(events=[], next_cursor="repeat", has_more=True)),
        (
            "list_cost_events",
            dict(items=[], next_cursor="repeat", has_more=True, window=WINDOW),
        ),
    ],
)
def test_repeated_cursor_fails_with_retained_continuation(monkeypatch, name, payload):
    result, requests = call(
        monkeypatch,
        name,
        [httpx.Response(200, json=payload), httpx.Response(200, json=payload)],
        {"all_pages": True},
    )
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_read_response"
    assert result.structured_content["recovery"]["cursor"] == "repeat"
    assert result.structured_content["data"]["next_cursor"] == "repeat"
    assert len(requests) == 2


@pytest.mark.parametrize("name", ["list_alert_activity", "list_open_alert_deliveries"])
def test_new_alert_result_limit_returns_original_cursor(monkeypatch, name):
    from solwyn_cli import _mcp_operation as operation

    payload = dict(
        events=[dict(EVENT, metadata_json={"large": "🧪" * 200_000})],
        next_cursor="successor",
    )
    if name == "list_alert_activity":
        payload["window"] = WINDOW
    assert len(httpx.Response(200, json=payload).content) < operation.MAX_RESPONSE_BYTES
    result, requests = call(
        monkeypatch, name, [httpx.Response(200, json=payload)], {"cursor": "original"}
    )
    assert result.is_error
    assert result.structured_content["error"]["code"] == "result_size_limit"
    assert result.structured_content["data"] is None
    assert result.structured_content["recovery"]["cursor"] == "original"
    assert len(requests) == 1


@pytest.mark.parametrize("verified", [False, True])
def test_jwt_identity_same_strict_read_and_verified_state(monkeypatch, verified):
    me = dict(
        user_id="user-synthetic",
        email="user@example.invalid",
        email_verified=verified,
        first_name="Synthetic",
        marketing_emails_subscribed=False,
        account_id="acct-synthetic",
        display_name="Synthetic",
        subscription_tier="free",
        alert_email_override=None,
    )
    account = dict(
        id="acct-synthetic",
        display_name="Synthetic",
        subscription_tier="free",
        alert_email_override=None,
        seat_limit=1,
        seats_used=1,
        your_role="member",
    )
    result, requests = call(
        monkeypatch,
        "get_whoami",
        [
            httpx.Response(200, json=me),
            httpx.Response(200, json=account),
            httpx.Response(200, json=[]),
        ],
    )
    assert not result.is_error, result
    data = result.structured_content["data"]
    assert data["user"] == me
    assert data["your_role"] == "member"
    assert [request[1] for request in requests] == [
        "/api/v1/account/me",
        "/api/v1/accounts/me",
        "/api/v1/projects",
    ]
    assert "Bearer synthetic" not in str(result)


def test_account_latest_tool_retains_both_firing_and_lifecycle(monkeypatch):
    fire = dict(
        EVENT,
        project_id=None,
        rule_key="account-one",
        rule_type="account_spend_threshold",
        event_type="fired",
    )
    paused = dict(
        fire, id="paused", event_type="paused", emitted_at="2026-10-01T00:00:00Z"
    )
    result, requests = call(
        monkeypatch,
        "get_account_alert",
        [
            httpx.Response(200, json=ACCOUNT),
            httpx.Response(200, json=dict(events=[paused, fire], window=WINDOW)),
        ],
        {"with_last_fired": True},
    )
    assert not result.is_error, result
    data = result.structured_content["data"]
    assert data["last_fired"]["id"] == fire["id"]
    assert data["latest_event"]["event_type"] == "paused"
    assert data["latest_window"] == WINDOW
    assert requests[1][2]["json"]["rules"] == [
        {
            "rule_key": "account-one",
            "project_id": None,
            "rule_type": "account_spend_threshold",
            "percent": None,
            "period_keys": None,
        }
    ]
