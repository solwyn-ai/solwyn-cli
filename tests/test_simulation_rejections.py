"""Public simulation calls retain definite-rejection retries and never replay a 401."""

import json

import anyio
import httpx
import pytest
from mcp import Client
from typer.testing import CliRunner

from solwyn_cli.client import MAX_RETRY_AFTER_SECONDS, SolwynClient
from solwyn_cli.commands import cost_pricing
from solwyn_cli.main import app
from solwyn_cli.mcp import server
from solwyn_cli import auth
from test_cost_advice_command import SIMULATION
from login_fixtures import fake_keychain, LOGIN_TOKEN, login_entries


@pytest.fixture
def keychain(monkeypatch):
    values = {}
    store = fake_keychain(monkeypatch)
    store.update(values)
    values = store
    return values


@pytest.fixture(params=["cli", "mcp"])
def simulation(monkeypatch, keychain, request):
    monkeypatch.delenv("SOLWYN_API_KEY", raising=False)
    monkeypatch.delenv("SOLWYN_AUTH", raising=False)
    keychain.update(login_entries(LOGIN_TOKEN))
    calls = []
    delays = []
    responses = []

    def handler(http_request):
        calls.append(http_request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def factory(*, api_url, credential, **options):
        return SolwynClient(
            api_url=api_url,
            credential=credential,
            transport=httpx.MockTransport(handler),
            sleeper=delays.append,
        )

    monkeypatch.setattr(cost_pricing, "SolwynClient", factory)
    monkeypatch.setattr(server, "SolwynClient", factory)

    def invoke(sequence):
        responses.extend(sequence)
        if request.param == "cli":
            result = CliRunner().invoke(
                app,
                [
                    "--json",
                    "--project",
                    "proj_alpha",
                    "costs",
                    "simulate",
                    "--provider",
                    "openai",
                    "--model",
                    "synthetic-model",
                ],
            )
            payload = json.loads(result.stderr if result.exit_code else result.stdout)
            return result.exit_code, payload

        async def exercise():
            async with Client(
                server.create_server(
                    launch_options=server.McpLaunchOptions(project_id="proj_alpha")
                ),
                cache=None,
            ) as client:
                return await client.call_tool(
                    "simulate_costs", {"provider": "openai", "model": "synthetic-model"}
                )

        result = anyio.run(exercise)
        payload = result.structured_content
        return int(result.is_error), payload["error"] if result.is_error else payload[
            "data"
        ]

    return invoke, calls, delays, keychain, request.param


def rejected(status):
    return httpx.Response(
        status, headers={"Retry-After": "999"}, json={"detail": "Rejected"}
    )


def test_simulation_401_is_never_replayed_with_the_stored_login(simulation):
    invoke, calls, delays, stored, interface = simulation
    status, payload = invoke([rejected(401)])
    assert status == (3 if interface == "cli" else 1)
    assert payload["code"] == (
        "authentication_failed" if interface == "cli" else "authentication_required"
    )
    assert [call.url.path for call in calls] == [
        "/api/v1/projects/proj_alpha/costs/simulate",
    ]
    assert calls[0].headers["Authorization"] == f"Bearer {LOGIN_TOKEN}"
    assert json.loads(stored[auth.LOGIN_ENTRY])["token"] == LOGIN_TOKEN
    assert delays == []


@pytest.mark.parametrize("exhausted", [False, True])
def test_simulation_429_uses_one_capped_capacity_retry(simulation, exhausted):
    invoke, calls, delays, _, _ = simulation
    status, payload = invoke(
        [
            rejected(429),
            rejected(429) if exhausted else httpx.Response(200, json=SIMULATION),
        ]
    )
    assert status == (1 if exhausted else 0)
    if exhausted:
        assert payload["code"] == "rate_limited"
    assert len(calls) == 2
    assert calls[0].content == calls[1].content
    assert delays == [MAX_RETRY_AFTER_SECONDS]


def test_simulation_401_after_capacity_retry_stops(simulation):
    invoke, calls, delays, _, interface = simulation
    status, payload = invoke([rejected(429), rejected(401)])
    assert status == (3 if interface == "cli" else 1)
    assert len(calls) == 2
    assert delays == [MAX_RETRY_AFTER_SECONDS]


@pytest.mark.parametrize(
    "failure", [rejected(503), httpx.ReadError("synthetic transport failure")]
)
def test_simulation_503_and_transport_never_replay(simulation, failure):
    invoke, calls, delays, stored, interface = simulation
    status, payload = invoke([failure])
    assert status == 1
    expected_code = (
        "service_busy"
        if isinstance(failure, httpx.Response)
        else ("transport_error" if interface == "cli" else "api_error")
    )
    assert payload["code"] == expected_code
    assert len(calls) == 1
    assert delays == []
    assert json.loads(stored[auth.LOGIN_ENTRY])["token"] == LOGIN_TOKEN
