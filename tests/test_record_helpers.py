"""Attribution transport and exact SDK tag parsing."""

from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from solwyn_cli.commands._tags import parse_tags, validate_tags
from solwyn_cli.commands._record import build_event, request_ingest


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
