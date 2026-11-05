"""Core busy handlers and proxy failures have different durability guarantees."""

import json

import anyio
import httpx
import pytest
from test_budget_batch import ALLOWED, CHECK, invoke, item
from test_mcp_conformance import _call_tool, _install_http
from test_reliability import BUSY, KEY
from typer.testing import CliRunner

from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import record
from solwyn_cli.commands._record import build_event, record_best_effort
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app

INGEST_DETAILS = [
    BUSY,
    "Account tag registry is busy; retry the batch.",
    "Accounting maintenance is in progress; retry the batch.",
]
CALL_ID = "12345678-1234-1234-1234-123456789abc"


def busy_response(detail=BUSY, retry_after="1"):
    return httpx.Response(
        503,
        headers={} if retry_after is None else {"Retry-After": retry_after},
        json={"detail": detail},
    )


@pytest.mark.parametrize("completed_chunks", [0, 1])
def test_legacy_busy_chunk_is_uncertain_and_suffix_is_unsubmitted(
    monkeypatch, completed_chunks
):
    responses = [
        httpx.Response(200, json={"results": [item(ALLOWED)] * 32})
        for _ in range(completed_chunks)
    ] + [busy_response()]
    result, payload, requests, remaining = invoke(
        monkeypatch, "cli", [CHECK] * 65, responses, no_record=True
    )
    assert result.exit_code == 1
    assert "Reservation state within the failed chunk is unknown" in result.stderr
    assert "later chunks were not submitted" in result.stderr.lower()
    assert "Nothing reserved" not in result.stderr
    assert "No reservation" not in result.stderr
    assert "safe to retry" not in result.stderr
    if completed_chunks:
        assert payload["failed_chunk_start"] == 32
        assert payload["total_checks"] == 65
        assert len(payload["results"]) == 32
        assert all(r["result"]["reservation_id"] == "res_1" for r in payload["results"])
        assert "do not replay completed checks" in result.stderr
    else:
        assert payload is None
        assert "Completed results" not in result.stderr
        assert "completed checks" not in result.stderr
    assert len(requests) == completed_chunks + 1
    assert not remaining


@pytest.mark.parametrize(
    "path", ["/api/v1/budgets/check", "/api/v1/budgets/check/batches"]
)
def test_sound_admission_hint_is_reservation_specific_and_preserves_tallies(path):
    requests = []
    body = {"uncounted_calls": 3, "uncounted_tokens": 4200}

    def handler(request):
        requests.append(request)
        return busy_response()

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("POST", path, json=body)
    assert "No reservation was made" in failure.value.hint
    assert "safe to retry" in failure.value.hint
    assert len(requests) == 1
    assert json.loads(requests[0].content) == body


@pytest.mark.parametrize("detail", INGEST_DETAILS)
def test_core_ingest_rejection_is_not_recorded_across_cli_and_mcp(monkeypatch, detail):
    requests, _, remaining = _install_http(
        monkeypatch,
        responses=[busy_response(detail), busy_response(detail)],
        credentials=[KEY],
    )
    result = anyio.run(
        _call_tool,
        "record_call",
        {
            "model": "local",
            "provider": "ollama",
            "call_id": CALL_ID,
        },
    )
    assert result.is_error
    recovery = result.structured_content["recovery"]
    assert recovery["recording"] == "not_recorded"
    assert recovery["call_id"] == CALL_ID
    assert "Nothing recorded" in result.structured_content["error"]["hint"]
    assert len(requests) == 2
    assert requests[0][2]["json"] == requests[1][2]["json"]
    assert not remaining

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(lambda _: busy_response(detail)),
        sleeper=lambda _: None,
    ) as client:
        recorded = record_best_effort(
            client=client,
            event=build_event(model="local", provider="ollama", call_id=CALL_ID),
        )
    assert recorded.status == "not_recorded"
    assert recorded.call_ids == [CALL_ID]


@pytest.mark.parametrize(
    "response",
    [
        busy_response(retry_after=None),
        busy_response(retry_after="0"),
        busy_response("busy"),
        busy_response({"code": "proxy_busy"}),
        httpx.Response(
            503, headers={"Retry-After": "1"}, text="<html>Unavailable</html>"
        ),
    ],
)
def test_unrecognized_ingest_failure_retains_identity_and_uncertainty(
    monkeypatch, response
):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[response, response],
        credentials=[KEY],
    )
    result = anyio.run(
        _call_tool,
        "record_call",
        {
            "model": "local",
            "provider": "ollama",
            "call_id": CALL_ID,
        },
    )
    assert result.is_error
    assert result.structured_content["recovery"]["recording"] == "unknown"
    assert result.structured_content["recovery"]["call_id"] == CALL_ID
    hint = result.structured_content["error"]["hint"]
    assert "Nothing recorded" not in hint
    assert "same call_ids" in hint
    assert len(requests) == (1 if "Retry-After" not in response.headers else 2)
    assert all(r[2]["json"][0]["call_id"] == CALL_ID for r in requests)

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(lambda _: response),
        sleeper=lambda _: None,
    ) as client:
        recorded = record_best_effort(
            client=client,
            event=build_event(model="local", provider="ollama", call_id=CALL_ID),
        )
    assert recorded.status == "failed"
    assert recorded.call_ids == [CALL_ID]


@pytest.mark.parametrize("completed_chunk", [False, True])
@pytest.mark.parametrize("known_rejection", [False, True])
def test_record_cli_retains_all_ids_and_completed_progress(
    monkeypatch, completed_chunk, known_rejection
):
    requests = []
    ids = [f"12345678-1234-1234-1234-{i:012x}" for i in range(1001)]
    events = [
        {"model": "local", "provider": "ollama", "call_id": value} for value in ids
    ]

    def handler(request):
        requests.append(request)
        if completed_chunk and len(requests) == 1:
            return httpx.Response(202, json={"ingested": 1000})
        return busy_response(BUSY if known_rejection else "Proxy unavailable")

    monkeypatch.setattr(record, "resolve_credential", lambda **_: KEY)
    monkeypatch.setattr(
        record,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            **kwargs,
            transport=httpx.MockTransport(handler),
            sleeper=lambda _: None,
        ),
    )
    result = CliRunner().invoke(
        app, ["--json", "record", "--batch", "-"], input=json.dumps(events)
    )
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["status"] == (
        "not_recorded" if known_rejection and not completed_chunk else "failed"
    )
    assert data["call_ids"] == ids
    assert data["ingested"] == (1000 if completed_chunk else 0)
    hint = json.loads(result.stderr.splitlines()[-1])["hint"]
    assert ("Nothing recorded" in hint) is known_rejection
    assert len(requests) == (3 if completed_chunk else 2)
    assert requests[-2].content == requests[-1].content
    assert [e["call_id"] for e in json.loads(requests[-1].content)] == (
        ids[1000:] if completed_chunk else ids[:1000]
    )


def test_simulate_busy_is_a_read_hint_without_post_replay():
    requests = []

    def handler(request):
        requests.append(request)
        return busy_response("Analytics capacity is busy; retry shortly")

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: pytest.fail("no POST replay"),
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("POST", "/api/v1/projects/p/costs/simulate")
    assert failure.value.code == "service_busy"
    assert failure.value.hint == "Service is busy; retry in a moment."
    assert len(requests) == 1
