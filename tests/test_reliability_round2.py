"""A logical ingest retains evidence from every HTTP attempt, offline."""

import json

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import budget, record
from solwyn_cli.commands._record import build_event, record_best_effort
from solwyn_cli.errors import ApiError, is_ingest_unaccepted
from solwyn_cli.main import app
from solwyn_cli.mcp import server
from test_budget_batch import CHECK, DENIED, invoke, item
from test_mcp_conformance import _call_tool, _install_http
from test_reliability import JWT, KEY
from test_reliability_review import CALL_ID, busy_response


def rejection(kind):
    if kind == "known":
        return busy_response("Account tag registry is busy; retry the batch.")
    if kind == "rate_limit":
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={"detail": "Rate limit exceeded", "retry_after": 60},
        )
    if kind == "auth":
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": "Bearer"},
            json={"detail": "Invalid or expired token"},
        )
    return busy_response("Proxy unavailable", retry_after="100")


@pytest.mark.parametrize("interface", ["cli", "mcp", "best_effort"])
@pytest.mark.parametrize(
    "first,last,absent",
    [
        ("unknown", "known", False),
        ("known", "known", True),
        ("rate_limit", "known", True),
        ("known", "unknown", False),
        ("unknown", "rate_limit", False),
        ("unknown", "auth", False),
    ],
)
def test_logical_ingest_rejection_state_and_hint(
    monkeypatch, interface, first, last, absent
):
    responses = [rejection(first), rejection(last)]
    if interface == "mcp":
        requests, _, remaining = _install_http(
            monkeypatch, responses=responses, credentials=[KEY]
        )
        result = anyio.run(
            _call_tool,
            "record_call",
            {"model": "local", "provider": "ollama", "call_id": CALL_ID},
        )
        assert result.is_error
        assert result.structured_content["recovery"]["recording"] == (
            "not_recorded" if absent else "unknown"
        )
        assert result.structured_content["recovery"]["call_id"] == CALL_ID
        hint = result.structured_content["error"]["hint"]
        bodies = [request[2]["json"] for request in requests]
        assert not remaining
    else:
        requests, waits = [], []

        def handler(request):
            requests.append(request)
            return responses.pop(0)

        def client_factory(**kwargs):
            return SolwynClient(
                **kwargs, transport=httpx.MockTransport(handler), sleeper=waits.append
            )

        if interface == "cli":
            monkeypatch.setattr(record, "resolve_credential", lambda: KEY)
            monkeypatch.setattr(record, "SolwynClient", client_factory)
            result = CliRunner().invoke(
                app,
                [
                    "--json",
                    "record",
                    "--model",
                    "local",
                    "--provider",
                    "ollama",
                    "--call-id",
                    CALL_ID,
                ],
            )
            assert result.exit_code == (3 if last == "auth" else 1)
            data = json.loads(result.stdout)
            assert data["status"] == ("not_recorded" if absent else "failed")
            assert data["call_ids"] == [CALL_ID]
            hint = json.loads(result.stderr)["hint"]
        else:
            with client_factory(
                api_url="https://example.test", credential=KEY
            ) as client:
                data = record_best_effort(
                    client=client,
                    event=build_event(
                        model="local", provider="ollama", call_id=CALL_ID
                    ),
                )
            assert data.status == ("not_recorded" if absent else "failed")
            assert data.call_ids == [CALL_ID]
            hint = None
        bodies = [json.loads(request.content) for request in requests]
        assert waits == [10.0 if first in {"unknown", "rate_limit"} else 1.0]
        assert not responses
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert bodies[0][0]["call_id"] == CALL_ID
    if hint is not None:
        assert ("Nothing recorded" in hint) is absent
        assert "same call_ids" in hint


@pytest.mark.parametrize("interface", ["cli", "mcp"])
@pytest.mark.parametrize("disposition", ["ingested", "deduplicated", "rejected"])
def test_canonical_success_resolves_earlier_ingest_uncertainty(
    monkeypatch, interface, disposition
):
    ack = {"ingested": 1}
    if disposition == "deduplicated":
        ack = {
            "ingested": 0,
            "duplicates": [{"index": 0, "reason": "call_id", "call_id": CALL_ID}],
        }
    elif disposition == "rejected":
        ack = {
            "ingested": 0,
            "rejected": [
                {
                    "index": 0,
                    "code": "unknown_model",
                    "model": "local",
                    "message": "Unknown model",
                }
            ],
        }
    responses = [rejection("unknown"), httpx.Response(202, json=ack)]
    if interface == "mcp":
        requests, _, _ = _install_http(
            monkeypatch, responses=responses, credentials=[KEY]
        )
        result = anyio.run(
            _call_tool,
            "record_call",
            {"model": "local", "provider": "ollama", "call_id": CALL_ID},
        )
        data = result.structured_content["data"]
        assert result.is_error is (disposition == "rejected")
        bodies = [request[2]["json"] for request in requests]
    else:
        requests = []

        def handler(request):
            requests.append(request)
            return responses.pop(0)

        monkeypatch.setattr(record, "resolve_credential", lambda: KEY)
        monkeypatch.setattr(
            record,
            "SolwynClient",
            lambda **kwargs: SolwynClient(
                **kwargs, transport=httpx.MockTransport(handler), sleeper=lambda _: None
            ),
        )
        result = CliRunner().invoke(
            app,
            [
                "--json",
                "record",
                "--model",
                "local",
                "--provider",
                "ollama",
                "--call-id",
                CALL_ID,
            ],
        )
        assert result.exit_code == (1 if disposition == "rejected" else 0)
        data = json.loads(result.stdout)
        bodies = [json.loads(request.content) for request in requests]
    assert data["status"] == disposition
    assert data["call_ids"] == [CALL_ID]
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize("first", ["unknown", "known", "rate_limit"])
@pytest.mark.parametrize("rotation_first", [False, True])
def test_ingest_evidence_survives_jwt_rotation_with_shared_retry_budget(
    monkeypatch, first, rotation_first
):
    # Core requires project keys for ingest. This deliberately synthetic sequence
    # protects the shared HTTP client's auth callback, without enabling JWT ingest.
    responses = [
        rejection(first),
        rejection("auth"),
        httpx.Response(
            200,
            json={
                "access_token": "synthetic-new-access",
                "refresh_token": "synthetic-new-refresh",
            },
        ),
        rejection("known"),
    ]
    if rotation_first:
        responses = [responses[1], responses[2], responses[0], responses[3]]
    requests, waits = [], []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access, refresh: auth.JwtCredential(
            access_token=access, refresh_token=refresh
        ),
    )
    with SolwynClient(
        api_url="https://example.test",
        credential=JWT,
        transport=httpx.MockTransport(handler),
        sleeper=waits.append,
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request(
                "POST",
                "/api/v1/metadata/ingest",
                json=[{"call_id": CALL_ID, "attempt_index": 7}],
            )
        assert client.current_credential.bearer_value() == "synthetic-new-access"
    assert is_ingest_unaccepted(failure.value) is (first != "unknown")
    assert ("Nothing recorded" in failure.value.hint) is (first != "unknown")
    assert len(requests) == 4
    ingest = [r for r in requests if r.url.path.endswith("/metadata/ingest")]
    assert len(ingest) == 3
    assert all(r.content == ingest[0].content for r in ingest)
    assert ingest[-1].headers["Authorization"] == "Bearer synthetic-new-access"
    assert waits == [10.0 if first != "known" else 1.0]
    assert not responses


@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_known_settlement_and_exact_retry_event_survive_mixed_failure(
    monkeypatch, interface
):
    requests, _, remaining = _install_http(
        monkeypatch,
        responses=[httpx.Response(204), rejection("unknown"), rejection("known")],
        credentials=[KEY],
    )
    values = {
        "reservation_id": "res_1",
        "call_id": CALL_ID,
        "model": "gpt-4o",
        "provider": "openai",
        "attempt_index": 1,
    }
    if interface == "mcp":
        result = anyio.run(_call_tool, "budget_confirm", values)
        assert result.is_error
        data = result.structured_content["data"]
        assert "Nothing recorded" not in result.structured_content["error"]["hint"]
    else:
        monkeypatch.setattr(budget, "resolve_credential", lambda: KEY)
        monkeypatch.setattr(budget, "SolwynClient", server.SolwynClient)
        args = ["--json", "budget", "confirm"]
        for name, value in values.items():
            args.extend(["--" + name.replace("_", "-"), str(value)])
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "Nothing recorded" not in result.stderr
    assert data["settlement"] == "confirmed"
    assert data["reservation_id"] == "res_1"
    assert data["call_id"] == CALL_ID
    assert data["recorded"]["status"] == ("failed" if interface == "cli" else "unknown")
    assert data["recording_retry"]["attempt_index"] == 1
    assert data["recording_retry"]["call_id"] == CALL_ID
    assert len(requests) == 3
    assert requests[-2][2]["json"] == requests[-1][2]["json"]
    sent = requests[-1][2]["json"][0]
    assert sent["timestamp"] == data["recording_retry"]["timestamp"]
    assert sent["sdk_instance_id"] == data["recording_retry"]["sdk_instance_id"]
    assert sent["attempt_index"] == 1
    assert not remaining


def test_completed_ingest_chunk_retains_dispositions_without_submitting_suffix(
    monkeypatch,
):
    ids = [f"12345678-1234-1234-1234-{i:012x}" for i in range(2001)]
    events = [
        {"model": "local", "provider": "ollama", "call_id": value, "attempt_index": 7}
        for value in ids
    ]
    responses = [
        httpx.Response(
            202,
            json={
                "ingested": 998,
                "rejected": [
                    {
                        "index": 0,
                        "code": "unknown_model",
                        "model": "local",
                        "message": "Unknown model",
                    }
                ],
                "duplicates": [{"index": 1, "reason": "call_id", "call_id": ids[1]}],
            },
        ),
        rejection("unknown"),
        rejection("known"),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(record, "resolve_credential", lambda: KEY)
    monkeypatch.setattr(
        record,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            **kwargs, transport=httpx.MockTransport(handler), sleeper=lambda _: None
        ),
    )
    result = CliRunner().invoke(
        app, ["--json", "record", "--batch", "-"], input=json.dumps(events)
    )
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["status"] == "failed"
    assert data["ingested"] == 998
    assert data["call_ids"] == ids
    assert data["rejected"][0]["index"] == 0
    assert data["duplicates"] == [{"index": 1, "reason": "call_id", "call_id": ids[1]}]
    hint = json.loads(result.stderr.splitlines()[-1])["hint"]
    assert "Nothing recorded" not in hint
    assert "same call_ids and attempt indices" in hint
    assert len(requests) == 3
    assert requests[-2].content == requests[-1].content
    replay = json.loads(requests[-1].content)
    assert [event["call_id"] for event in replay] == ids[1000:2000]
    assert all(event["attempt_index"] == 7 for event in replay)
    assert not responses


def test_ingest_uncertainty_and_retry_budget_are_scoped_to_one_logical_call():
    responses = [
        rejection("unknown"),
        rejection("known"),
        rejection("known"),
        rejection("known"),
    ]
    requests, waits = [], []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=waits.append,
    ) as client:
        event = build_event(model="local", provider="ollama", call_id=CALL_ID)
        first = record_best_effort(client=client, event=event)
        second = record_best_effort(client=client, event=event)
    assert first.status == "failed"
    assert second.status == "not_recorded"
    assert first.call_ids == second.call_ids == [CALL_ID]
    assert len(requests) == 4
    assert all(r.content == requests[0].content for r in requests)
    assert waits == [10.0, 1.0]
    assert not responses


def test_terminal_transport_failure_keeps_earlier_ingest_evidence():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return rejection("unknown")
        raise httpx.ReadError("Synthetic connection loss")

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request(
                "POST", "/api/v1/metadata/ingest", json=[{"call_id": CALL_ID}]
            )
    assert not is_ingest_unaccepted(failure.value)
    assert failure.value.ingest_outcome_uncertain
    assert "same call_ids and attempt indices" in failure.value.hint
    assert len(requests) == 2
    assert requests[0].content == requests[1].content


@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_known_denial_survives_mixed_ingest_failure(monkeypatch, interface):
    result, payload, requests, remaining = invoke(
        monkeypatch,
        interface,
        [CHECK],
        [
            httpx.Response(200, json={"results": [item(DENIED)]}),
            rejection("unknown"),
            rejection("known"),
        ],
    )
    data = payload if interface == "cli" else payload["data"]
    outcome = data["results"][0]["result"]
    assert outcome["allowed"] is False
    assert outcome["recorded"]["status"] == (
        "failed" if interface == "cli" else "unknown"
    )
    assert len(outcome["recorded"]["call_ids"]) == 1
    assert len(requests) == 3
    assert requests[-2][2]["json"] == requests[-1][2]["json"]
    assert not remaining
    if interface == "cli":
        assert result.exit_code == 4
