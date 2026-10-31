"""Observed media reaches settlement and attribution without estimate substitution."""

import json
import threading
import time

import anyio
import httpx
import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli import _mcp_operation as operation
from solwyn_cli._vendor.wire import (
    BudgetConfirmRequest,
    IngestMetadataEvent,
    TokenDetails,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import budget
from solwyn_cli.commands import _record
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app
from solwyn_cli.mcp import server as mcp_server
from test_mcp_conformance import VALID_KEY, _call_tool, _install_http
from test_mcp_sdk_migration import raw_connection


CALL_ID = "12345678-1234-4000-8000-123456789abc"
CONFIRM = {
    "reservation_id": "res-observed",
    "call_id": CALL_ID,
    "model": "whisper-1",
    "provider": "openai",
}
KEY = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")


@pytest.mark.parametrize(
    "media",
    [
        {"audio_seconds": 120.5, "is_estimated": False},
        {
            "image_count": 2,
            "resolution": "1024x1024",
            "quality": "hd",
            "is_estimated": True,
        },
        {"video_seconds": 8, "generation_count": 1, "resolution": "4k"},
        {"input_characters": 1200, "is_estimated": True},
        {"audio_seconds": 0},
        {"audio_seconds": None},
        None,
    ],
)
def test_observed_media_is_identical_on_both_http_writes(media):
    sent = []

    def handle(request):
        sent.append((request.url.path, json.loads(request.content)))
        return (
            httpx.Response(204)
            if len(sent) == 1
            else httpx.Response(202, json={"ingested": 1})
        )

    request = BudgetConfirmRequest(
        **CONFIRM, token_details=TokenDetails(), media_usage=media
    )
    event = budget.confirm_event(request=request, modality="audio")
    with SolwynClient(
        api_url="https://test.invalid",
        credential=KEY,
        transport=httpx.MockTransport(handle),
    ) as client:
        result = budget.confirm_and_record(client=client, request=request, event=event)
    assert result.recorded.status == "ingested"
    assert [path for path, _ in sent] == [
        "/api/v1/budgets/confirm",
        "/api/v1/metadata/ingest",
    ]
    confirm, ingest = sent[0][1], sent[1][1][0]
    if media is None:
        assert "media_usage" not in confirm and "media_usage" not in ingest
    else:
        expected = {
            "is_estimated": False,
            **{key: value for key, value in media.items() if value is not None},
        }
        assert confirm["media_usage"] == ingest["media_usage"] == expected
    assert ingest["modality"] == "audio"
    assert confirm["call_id"] == ingest["call_id"] == CALL_ID


def test_confirm_event_preserves_served_fallback_and_token_subsets():
    request = BudgetConfirmRequest(
        **{**CONFIRM, "model": "claude-sonnet-4-20250514", "provider": "bedrock"},
        provider_region="eu-west-1",
        service_tier="priority",
        is_provider_fallback=True,
        token_details=TokenDetails(
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=10,
            cache_creation_5m_tokens=20,
            cache_creation_1h_tokens=30,
            reasoning_tokens=5,
            audio_input_tokens=4,
            audio_output_tokens=3,
            image_input_tokens=2,
            image_output_tokens=1,
        ),
    )
    event = budget.confirm_event(request=request)
    assert event.is_provider_fallback is True
    assert event.provider == "bedrock" and event.model == request.model
    assert event.provider_region == "eu-west-1" and event.service_tier == "priority"
    assert event.token_details == request.token_details
    assert event.input_tokens == 100 and event.output_tokens == 50
    assert event.attempt_index == 0


@pytest.mark.parametrize("no_record", [False, True])
def test_mcp_confirmation_passes_observed_media_and_modality(monkeypatch, no_record):
    requests, _, remaining = _install_http(
        monkeypatch,
        responses=[httpx.Response(204)]
        + ([] if no_record else [httpx.Response(202, json={"ingested": 1})]),
        credentials=[KEY],
    )
    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {
            **CONFIRM,
            "media_usage": {"audio_seconds": 120, "is_estimated": True},
            "modality": "audio",
            "no_record": no_record,
        },
    )
    assert not result.is_error, result.structured_content
    assert not remaining
    assert requests[0][2]["json"]["media_usage"] == {
        "audio_seconds": 120.0,
        "is_estimated": True,
    }
    if not no_record:
        event = requests[1][2]["json"][0]
        assert event["media_usage"] == requests[0][2]["json"]["media_usage"]
        assert event["modality"] == "audio"
    assert result.structured_content["data"]["recorded"]["status"] == (
        "skipped" if no_record else "ingested"
    )


@pytest.mark.parametrize(
    "media",
    [
        {"audio_seconds": -1},
        {"image_count": 100_000_001},
        {"resolution": "PRIVATE_CANARY" * 4},
        {"quality": {"PRIVATE_CANARY": 1}},
        {"audio_seconds": "PRIVATE_CANARY"},
        {"audio_second": 3},
    ],
)
def test_mcp_media_validation_is_private_and_precedes_http(monkeypatch, caplog, media):
    requests, _, _ = _install_http(monkeypatch, responses=[], credentials=[KEY])
    result = anyio.run(_call_tool, "budget_confirm", {**CONFIRM, "media_usage": media})
    assert result.is_error
    assert "PRIVATE_CANARY" not in result.model_dump_json() + caplog.text
    assert not requests


@pytest.mark.parametrize(
    "flags, expected",
    [
        (["--audio-seconds", "0"], {"audio_seconds": 0.0, "is_estimated": False}),
        (
            [
                "--image-count",
                "2",
                "--resolution",
                "1024x1024",
                "--quality",
                "hd",
                "--is-estimated",
            ],
            {
                "image_count": 2,
                "resolution": "1024x1024",
                "quality": "hd",
                "is_estimated": True,
            },
        ),
        (
            ["--video-seconds", "8", "--generation-count", "1"],
            {"video_seconds": 8.0, "generation_count": 1, "is_estimated": False},
        ),
        (
            ["--input-characters", "500"],
            {"input_characters": 500, "is_estimated": False},
        ),
    ],
)
def test_cli_observed_media_flags_reach_both_writes(monkeypatch, flags, expected):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return (
            httpx.Response(204)
            if len(requests) == 1
            else httpx.Response(202, json={"ingested": 1})
        )

    monkeypatch.setattr(budget, "resolve_credential", lambda **_: KEY)
    monkeypatch.setattr(
        budget,
        "SolwynClient",
        lambda **kwargs: SolwynClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    args = ["--json", "budget", "confirm"]
    for name, value in CONFIRM.items():
        args.extend(["--" + name.replace("_", "-"), value])
    result = CliRunner().invoke(app, [*args, "--modality", "audio", *flags])
    assert result.exit_code == 0, result.output
    assert requests[0]["media_usage"] == requests[1][0]["media_usage"] == expected
    assert requests[1][0]["modality"] == "audio"


def test_known_settlement_recovery_reposts_exact_event_without_confirm(monkeypatch):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(204), httpx.Response(503)],
        credentials=[KEY],
    )
    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {
            **CONFIRM,
            "media_usage": {"audio_seconds": 120},
            "modality": "audio",
            "attempt_index": 1,
            "is_provider_fallback": True,
            "tags": {"run": "original"},
        },
    )
    assert result.is_error
    assert "budget_confirm record_only" in result.structured_content["error"]["hint"]
    assert "FIRST" in result.structured_content["error"]["hint"]
    data = result.structured_content["data"]
    assert data["settlement"] == "confirmed"
    original = data["recording_retry"]
    assert original["attempt_index"] == 1 and original["is_provider_fallback"] is True
    sent = requests[1][2]["json"][0]
    assert sent["timestamp"] == original["timestamp"]
    assert sent["sdk_instance_id"] == original["sdk_instance_id"]
    assert sent["media_usage"] == {"audio_seconds": 120.0, "is_estimated": False}
    monkeypatch.setenv("SOLWYN_TAGS", "run=changed")
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "changed")
    retried, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(202, json={"ingested": 1})],
        credentials=[KEY],
    )
    retry = anyio.run(_call_tool, "budget_confirm", {"record_only": original})
    assert not retry.is_error, retry.structured_content
    assert [(method, path) for method, path, _ in retried] == [
        ("POST", "/api/v1/metadata/ingest")
    ]
    assert retried[0][2]["json"] == requests[1][2]["json"]
    assert retry.structured_content["data"]["settlement"] == "not_submitted"
    assert retry.structured_content["data"]["reservation_id"] is None
    assert retry.structured_content["recovery"]["settlement"] == "not_submitted"


def test_standalone_record_failure_preserves_its_recovery_guidance(monkeypatch):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                202,
                json={
                    "ingested": 0,
                    "duplicates": [
                        {"index": 0, "reason": "legacy_key", "call_id": CALL_ID}
                    ],
                },
            )
        ],
        credentials=[KEY],
    )
    result = anyio.run(
        _call_tool,
        "record_call",
        {"call_id": CALL_ID, "model": "whisper-1", "provider": "openai"},
    )
    assert result.is_error
    hint = result.structured_content["error"]["hint"]
    assert "same call_id and attempt" in hint
    assert "budget_confirm" not in hint
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/api/v1/metadata/ingest")
    ]


def retry_event():
    return {
        "model": "whisper-1",
        "provider": "openai",
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 15,
        "status": "success",
        "is_model_fallback": False,
        "call_id": CALL_ID,
        "sdk_instance_id": "caller-retained-instance",
        "timestamp": "2026-09-06T12:00:00Z",
        "media_usage": {"audio_seconds": 120},
        "modality": "audio",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"reservation_id": "res"},
        {"call_id": CALL_ID},
        {"model": "whisper-1"},
        {"provider": "openai"},
        {"no_record": True},
        {"no_record": False},
        {"input_tokens": 0},
        {"tags": {}},
    ],
)
def test_record_only_rejects_mixed_modes_before_auth(monkeypatch, extra):
    requests, clients, _ = _install_http(monkeypatch, responses=[], credentials=[])
    result = anyio.run(
        _call_tool, "budget_confirm", {"record_only": retry_event(), **extra}
    )
    assert result.is_error
    assert (
        result.structured_content["error"]["code"] == "invalid_budget_confirm_request"
    )
    assert not requests and not clients


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.pop("timestamp"),
        lambda event: event.update(latency_ms=-1),
        lambda event: event.update(token_details={"input_tokens": 1}),
        lambda event: event.update(attempt_index=-1),
        lambda event: event.update(status="budget_denied"),
        lambda event: event.update(media_usage={"audio_seconds": "PRIVATE_CANARY"}),
        lambda event: event.update(unrecognized="PRIVATE_CANARY"),
    ],
)
def test_record_only_validates_exact_event_before_effects(monkeypatch, caplog, mutate):
    event = retry_event()
    mutate(event)
    requests, clients, _ = _install_http(monkeypatch, responses=[], credentials=[])
    result = anyio.run(_call_tool, "budget_confirm", {"record_only": event})
    assert result.is_error
    assert "PRIVATE_CANARY" not in result.model_dump_json() + caplog.text
    assert not requests and not clients


def test_cli_record_only_consumes_exact_event_and_rejects_other_flags(
    monkeypatch, tmp_path
):
    sent = []
    event = retry_event()
    event.update(attempt_index=1, is_provider_fallback=True)
    path = tmp_path / "retry.json"
    path.write_text(json.dumps(event))
    monkeypatch.setenv("SOLWYN_TAGS", "changed=ambient")
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "changed")
    monkeypatch.setattr(budget, "resolve_credential", lambda **_: KEY)

    def handle(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"ingested": 1})

    monkeypatch.setattr(
        budget,
        "SolwynClient",
        lambda **kwargs: SolwynClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    result = CliRunner().invoke(
        app, ["--json", "budget", "confirm", "--record-only", str(path)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["settlement"] == "not_submitted"
    assert len(sent) == 1 and sent[0][0] == "/api/v1/metadata/ingest"
    body = sent[0][1][0]
    assert all(
        body[key] == value for key, value in event.items() if key != "media_usage"
    )
    assert body["media_usage"] == {"audio_seconds": 120.0, "is_estimated": False}
    assert "tags" not in body and "agent_run_id" not in body
    for extra in (["--no-record"], ["--input-tokens", "0"], ["--call-id", CALL_ID]):
        result = CliRunner().invoke(
            app, ["--json", "budget", "confirm", "--record-only", str(path), *extra]
        )
        assert result.exit_code == 2
    assert len(sent) == 1


def test_media_event_is_retained_at_confirm_to_ingest_cancellation_boundary(
    monkeypatch,
):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.07)
    original = _record.request_ingest
    finished = threading.Event()

    def delayed(**kwargs):
        try:
            time.sleep(0.12)
            return original(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(_record, "request_ingest", delayed)
    requests, _, _ = _install_http(
        monkeypatch, responses=[httpx.Response(204)], credentials=[KEY]
    )
    result = anyio.run(
        _call_tool,
        "budget_confirm",
        {**CONFIRM, "media_usage": {"audio_seconds": 120}, "modality": "audio"},
    )
    assert result.is_error
    assert result.structured_content["recovery"]["settlement"] == "confirmed"
    data = result.structured_content["data"]
    assert data["settlement"] == "confirmed"
    assert data["recording_retry"]["call_id"] == CALL_ID
    assert data["recording_retry"]["media_usage"]["audio_seconds"] == 120
    assert finished.wait(1)
    assert len(requests) == 1


def test_lost_confirmation_reply_retains_unknown_state_and_original_event():
    requests = []
    request = BudgetConfirmRequest(
        **CONFIRM, token_details=TokenDetails(), media_usage={"audio_seconds": 120}
    )
    event = budget.confirm_event(request=request, modality="audio", attempt_index=1)

    async def handle(http_request):
        requests.append(http_request.url.path)
        raise httpx.ReadError("PRIVATE_CANARY", request=http_request)

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handle)
        ) as control:

            def run():
                with SolwynClient(
                    api_url="https://test.invalid",
                    credential=KEY,
                    transport=control.transport(),
                    sleeper=control.sleep,
                ) as client:
                    return budget.confirm_and_record(
                        client=client, request=request, event=event
                    )

            with pytest.raises(ApiError) as caught:
                await operation.run_sync(run)
            assert caught.value.code == "transport_error"
            snapshot = control.snapshot()
            assert snapshot.recovery.settlement == snapshot.data.settlement == "unknown"
            assert snapshot.recovery.recording == "not_submitted"
            assert snapshot.data.recording_retry == event
            assert snapshot.data.recording_retry.attempt_index == 1

    anyio.run(exercise)
    assert requests == ["/api/v1/budgets/confirm"]


@pytest.mark.parametrize("settlement_only", [True, False])
def test_public_lost_confirm_recovery_preserves_first_event_only_with_explicit_modes(
    monkeypatch, settlement_only
):
    sent = []
    _install_http(monkeypatch, responses=[], credentials=[KEY, KEY, KEY])

    def handle(request):
        sent.append((request.url.path, bytes(request.content)))
        if len(sent) == 1:
            raise httpx.ReadError("PRIVATE_CANARY", request=request)
        return (
            httpx.Response(204)
            if request.url.path == "/api/v1/budgets/confirm"
            else httpx.Response(202, json={"ingested": 1})
        )

    monkeypatch.setattr(
        mcp_server,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            api_url=kwargs["api_url"],
            credential=kwargs["credential"],
            transport=httpx.MockTransport(handle),
        ),
    )
    monkeypatch.setenv("SOLWYN_TAGS", "phase=first")
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "first-run")
    args = {
        **CONFIRM,
        "media_usage": {"audio_seconds": 120.5},
        "modality": "audio",
        "attempt_index": 1,
        "is_provider_fallback": True,
    }
    first = anyio.run(_call_tool, "budget_confirm", args)
    assert first.is_error
    assert first.structured_content["data"]["settlement"] == "unknown"
    assert first.structured_content["recovery"]["recording"] == "not_submitted"
    original = first.structured_content["data"]["recording_retry"]
    expected = httpx.Request(
        "POST",
        "https://test.invalid/api/v1/metadata/ingest",
        json=[
            IngestMetadataEvent.model_validate(original).model_dump(
                mode="json", exclude_none=True
            )
        ],
    ).content
    assert [path for path, _ in sent] == ["/api/v1/budgets/confirm"]
    monkeypatch.setenv("SOLWYN_TAGS", "phase=replay")
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "replay-run")
    replay = anyio.run(
        _call_tool,
        "budget_confirm",
        {**args, "no_record": True} if settlement_only else args,
    )
    assert not replay.is_error
    assert replay.structured_content["data"]["settlement"] == "confirmed"
    replacement = replay.structured_content["data"]["recording_retry"]
    assert replacement["timestamp"] != original["timestamp"]
    assert replacement["sdk_instance_id"] != original["sdk_instance_id"]
    assert replacement["tags"] == {"phase": "replay"}
    assert replacement["agent_run_id"] == "replay-run"
    assert sent[0] == sent[1]  # no_record never changes settlement HTTP bytes.
    if settlement_only:
        assert replay.structured_content["data"]["recorded"]["status"] == "skipped"
        assert len(sent) == 2
        recorded = anyio.run(_call_tool, "budget_confirm", {"record_only": original})
        assert not recorded.is_error
        assert recorded.structured_content["data"]["settlement"] == "not_submitted"
        assert recorded.structured_content["data"]["recording_retry"] == original
        assert sent[-1][1] == expected
    else:
        # Negative control: ordinary fresh-confirm replay ingests a new event.
        assert sent[-1][1] != expected
        assert (
            json.loads(sent[-1][1])[0]["sdk_instance_id"]
            == replacement["sdk_instance_id"]
        )
    assert [path for path, _ in sent] == [
        "/api/v1/budgets/confirm",
        "/api/v1/budgets/confirm",
        "/api/v1/metadata/ingest",
    ]
    hint = first.structured_content["error"]["hint"] or ""
    assert "no_record=true" in hint and "known 204" in hint
    assert "FIRST" in hint and "unused" in hint
    assert "PRIVATE_CANARY" not in first.model_dump_json()


@pytest.mark.parametrize("legacy", [False, True])
def test_public_confirm_catalog_explains_first_event_recovery(legacy):
    async def exercise():
        async with raw_connection(mcp_server.create_server(), legacy=legacy) as (
            request,
            _,
        ):
            tools = (await request("tools/list"))["result"]["tools"]
            confirm = next(tool for tool in tools if tool["name"] == "budget_confirm")
            description = confirm["description"]
            assert "no_record=true" in description and "known 204" in description
            assert "FIRST" in description and "unused" in description
            properties = confirm["inputSchema"]["properties"]
            assert "FIRST" in properties["record_only"]["description"]
            assert "lost 204" in properties["no_record"]["description"]

    anyio.run(exercise)


def test_cli_help_states_first_event_recovery_and_lost_reply_limit():
    result = CliRunner().invoke(app, ["budget", "confirm", "--help"])
    assert result.exit_code == 0
    help_text = " ".join(strip_ansi(result.output).split())
    assert "--no-record" in help_text and "FIRST" in help_text
    assert "unused new event" in help_text
    assert "CLI transport errors do not return the prepared event" in help_text


def test_cli_lost_confirm_error_does_not_return_unseen_event(monkeypatch):
    sent = []

    def handle(request):
        sent.append(request.url.path)
        raise httpx.ReadError("PRIVATE_CANARY", request=request)

    monkeypatch.setattr(budget, "resolve_credential", lambda **_: KEY)
    monkeypatch.setattr(
        budget,
        "SolwynClient",
        lambda **kwargs: SolwynClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    flags = ["--json", "budget", "confirm"]
    for name, value in CONFIRM.items():
        flags.extend(["--" + name.replace("_", "-"), value])
    result = CliRunner().invoke(app, [*flags, "--audio-seconds", "120.5"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["code"] == "transport_error"
    assert "data" not in payload and "recovery" not in payload
    assert "recording_retry" not in result.stderr
    assert "PRIVATE_CANARY" not in result.stderr
    assert sent == ["/api/v1/budgets/confirm"]


@pytest.mark.parametrize("missing", ["reservation_id", "call_id", "model", "provider"])
def test_fresh_confirm_requires_identity_before_auth(monkeypatch, missing):
    args = {name: value for name, value in CONFIRM.items() if name != missing}
    requests, clients, _ = _install_http(monkeypatch, responses=[], credentials=[])
    result = anyio.run(_call_tool, "budget_confirm", args)
    assert result.is_error
    assert (
        result.structured_content["error"]["code"] == "invalid_budget_confirm_request"
    )
    assert not requests and not clients


@pytest.mark.parametrize(
    "flags",
    [
        ["--audio-seconds", "-1"],
        ["--image-count", "100000001"],
        ["--video-seconds", "nan"],
        ["--input-characters", "-1"],
    ],
)
def test_cli_invalid_media_is_rejected_before_auth(monkeypatch, flags):
    monkeypatch.setattr(
        budget,
        "resolve_credential",
        lambda **_: pytest.fail("invalid media reached auth"),
    )
    args = ["--json", "budget", "confirm"]
    for name, value in CONFIRM.items():
        args.extend(["--" + name.replace("_", "-"), value])
    result = CliRunner().invoke(app, [*args, *flags])
    assert result.exit_code == 2


@pytest.mark.parametrize("kind", ["record_only", "media_flags"])
def test_cli_invalid_observations_do_not_echo_private_values(
    monkeypatch, tmp_path, kind
):
    monkeypatch.setattr(
        budget,
        "resolve_credential",
        lambda **_: pytest.fail("invalid observations reached auth"),
    )
    args = ["--json", "budget", "confirm"]
    if kind == "record_only":
        event = retry_event()
        event["media_usage"] = {"audio_seconds": "PRIVATE_CANARY"}
        path = tmp_path / "event.json"
        path.write_text(json.dumps(event))
        args.extend(["--record-only", str(path)])
    else:
        for name, value in CONFIRM.items():
            args.extend(["--" + name.replace("_", "-"), value])
        args.extend(["--resolution", "PRIVATE_CANARY" * 4])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
