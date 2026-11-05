"""Attribution transport and exact SDK tag parsing."""

from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from solwyn_cli.auth import ApiKeyCredential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._record import build_event, request_ingest
from solwyn_cli.commands._tags import parse_tags, validate_tags


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("env=prod,expr=left=right", {"env": "prod", "expr": "left=right"}),
        (" env = prod ", {" env ": " prod "}),
        ("environment=", {"environment": ""}),
        ("a=1,a=2", {"a": "2"}),
    ],
)
def test_sdk_tags(raw, expected):
    assert parse_tags(environ={"SOLWYN_TAGS": raw}) == expected


@pytest.mark.parametrize(
    "raw", ["a", "a=1,", "a=1,,b=2", "", "=v", "desc=alpha,beta", "a=\0"]
)
def test_invalid_sdk_tags(raw):
    with pytest.raises(ValueError):
        parse_tags(environ={"SOLWYN_TAGS": raw})


def test_flags_override_environment_and_note_duplicates(capsys):
    assert parse_tags(["a=2"], environ={"SOLWYN_TAGS": "a=1,b=3"}) == {
        "a": "2",
        "b": "3",
    }
    assert "last value wins" in capsys.readouterr().err


@pytest.mark.parametrize(
    "tags",
    [
        {str(i): "" for i in range(11)},
        {"a" * 65: ""},
        {"a": "x" * 257},
        {"a": 1},
        {"a\0": ""},
    ],
)
def test_tag_mapping_bounds(tags):
    with pytest.raises(ValueError):
        validate_tags(tags)


def test_flag_comma_is_invalid():
    with pytest.raises(ValueError):
        parse_tags(["a=one,two"], environ={})


class Client:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return httpx.Response(202, json=self.body)


def test_event_identity_and_no_cost_or_lease():
    first = build_event(
        model="local", provider="ollama", input_tokens=1, output_tokens=2
    )
    second = build_event(
        model="local", provider="ollama", input_tokens=1, output_tokens=2
    )
    assert UUID(first.sdk_instance_id).version == 4
    assert first.sdk_instance_id != second.sdk_instance_id
    assert first.call_id != second.call_id
    assert first.timestamp.tzinfo is not None
    body = first.model_dump(mode="json", exclude_none=True)
    assert "cost_usd" not in body and "lease_id" not in body
    for extra in ("cost_usd", "lease_id"):
        with pytest.raises(ValidationError):
            build_event(model="local", provider="ollama", **{extra: "value"})


@pytest.mark.parametrize(
    "call_id",
    [
        "call-1",
        "12345678-1234-1234-1234-123456789ABC",
        "{12345678-1234-1234-1234-123456789abc}",
    ],
)
def test_call_id_is_canonical(call_id):
    with pytest.raises(ValueError, match="uuidgen"):
        build_event(model="local", provider="ollama", call_id=call_id)


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"ingested": 1}, "ingested"),
        (
            {
                "ingested": 0,
                "rejected": [
                    {
                        "index": 0,
                        "code": "unknown_model",
                        "model": "local",
                        "message": "Use the exact model name.",
                    }
                ],
            },
            "rejected",
        ),
        (
            {
                "ingested": 0,
                "duplicates": [
                    {
                        "index": 0,
                        "reason": "call_id",
                        "call_id": "12345678-1234-1234-1234-123456789abc",
                    }
                ],
            },
            "deduplicated",
        ),
        (
            {
                "ingested": 0,
                "duplicates": [
                    {
                        "index": 0,
                        "reason": "legacy_key",
                        "call_id": "12345678-1234-1234-1234-123456789abc",
                    }
                ],
            },
            "failed",
        ),
    ],
)
def test_explicit_dispositions(body, status, capsys):
    event = build_event(
        model="local", provider="ollama", call_id="12345678-1234-1234-1234-123456789abc"
    )
    client = Client(body)
    result = request_ingest(client=client, events=[event])
    assert result.status == status
    assert result.duplicates == body.get("duplicates", []) or [
        d.model_dump() for d in result.duplicates
    ] == body.get("duplicates", [])
    assert client.calls[0][0] == ("POST", "/api/v1/metadata/ingest")
    assert client.calls[0][1]["json"] == [
        event.model_dump(mode="json", exclude_none=True)
    ]
    if status == "rejected":
        assert "Use the exact model name." in capsys.readouterr().err


def test_token_details_fill_top_level_counts():
    event = build_event(
        model="gpt-4o",
        provider="openai",
        token_details={"input_tokens": 12, "output_tokens": 3},
    )
    assert event.input_tokens == 12 and event.output_tokens == 3


def test_conflicting_top_level_usage_is_rejected():
    with pytest.raises(ValueError, match="match"):
        build_event(
            model="gpt-4o",
            provider="openai",
            input_tokens=7,
            token_details={"input_tokens": 12},
        )


@pytest.mark.parametrize("latency", [float("nan"), float("inf"), -1])
def test_invalid_latency_is_rejected(latency):
    with pytest.raises(ValueError):
        build_event(model="gpt-4o", provider="openai", latency_ms=latency)


@pytest.mark.parametrize(
    "body",
    [
        {"ingested": 0},
        {"ingested": 2},
        {
            "ingested": 0,
            "duplicates": [
                {
                    "index": 1,
                    "reason": "call_id",
                    "call_id": "12345678-1234-1234-1234-123456789abc",
                }
            ],
        },
        {
            "ingested": 0,
            "duplicates": [
                {
                    "index": 0,
                    "reason": "call_id",
                    "call_id": "12345678-1234-1234-1234-123456789abd",
                }
            ],
        },
    ],
)
def test_ambiguous_or_mismatched_dispositions_fail_closed(body):
    with pytest.raises(ValueError):
        request_ingest(
            client=Client(body),
            events=[
                build_event(
                    model="llama3",
                    provider="ollama",
                    call_id="12345678-1234-1234-1234-123456789abc",
                )
            ],
        )


def test_ingest_429_uses_existing_bounded_retry():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"detail": "Slow down"}
            )
        return httpx.Response(202, json={"ingested": 1})

    with SolwynClient(
        api_url="https://api.example.test",
        credential=ApiKeyCredential(
            api_key="solwyn_sk_" + "a1" * 32, source="environment"
        ),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    ) as client:
        event = build_event(model="llama3", provider="ollama")
        result = request_ingest(client=client, events=[event])
    assert result.status == "ingested" and len(requests) == 2
    assert requests[0].content == requests[1].content
