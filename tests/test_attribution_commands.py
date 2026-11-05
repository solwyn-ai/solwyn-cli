"""Recording defaults and standalone attribution behavior."""

import json
from pathlib import Path

import httpx
import pytest
from golden.harness import assert_matches_golden
from typer.testing import CliRunner

from solwyn_cli.commands import budget
from solwyn_cli.errors import ApiError, error_from_response
from solwyn_cli.main import app

GOLDEN_DIR = Path(__file__).parent / "golden"
runner = CliRunner()
CALL_ID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def transport(monkeypatch):
    requests = []
    replies = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            requests.append((method, path, kwargs))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setenv("SOLWYN_API_KEY", "solwyn_sk_" + "f6" * 32)
    monkeypatch.delenv("SOLWYN_TAGS", raising=False)
    monkeypatch.setattr(budget, "SolwynClient", Client)
    # Additional command modules are registered after the first red run.
    from solwyn_cli.commands import record

    monkeypatch.setattr(record, "SolwynClient", Client)
    return requests, replies


CONFIRM = [
    "budget",
    "confirm",
    "--reservation-id",
    "res_1",
    "--call-id",
    CALL_ID,
    "--model",
    "gpt-4o",
    "--provider",
    "openai",
]
CHECK = [
    "budget",
    "check",
    "--estimated-input-tokens",
    "21",
    "--model",
    "gpt-4o",
    "--provider",
    "openai",
]
DENIED = dict(
    allowed=False,
    remaining_budget=0,
    reservation_id=None,
    mode="hard_deny",
    budget_limit=10,
    current_usage=10,
    denied_by_period="tag",
    project_id="proj_356a192b7913b04c54574d18",
)


def test_confirm_records_same_call_and_keeps_attribution_out_of_confirm(transport):
    requests, replies = transport
    replies.extend([httpx.Response(204), httpx.Response(202, json={"ingested": 1})])
    result = runner.invoke(
        app,
        [
            "--json",
            *CONFIRM,
            "--tag",
            "env=prod",
            "--agent-run-id",
            "job",
            "--parent-run-id",
            "parent",
            "--run-name",
            "Test",
            "--input-tokens",
            "12",
            "--latency-ms",
            "50",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["recorded"]["status"] == "ingested"
    confirm, ingest = requests[0][2]["json"], requests[1][2]["json"][0]
    assert confirm["call_id"] == ingest["call_id"] == CALL_ID
    assert "tags" not in confirm and "agent_run_id" not in confirm
    assert ingest["input_tokens"] == 12 and ingest["tags"] == {"env": "prod"}
    assert ingest["parent_agent_run_id"] == "parent"


@pytest.mark.parametrize("no_record", [False, True])
def test_confirm_record_failure_does_not_undo_settlement(transport, no_record):
    requests, replies = transport
    replies.extend([httpx.Response(204), ValueError("offline")])
    result = runner.invoke(
        app, ["--json", *CONFIRM, *(["--no-record"] if no_record else [])]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["recorded"]["status"] == (
        "skipped" if no_record else "failed"
    )
    assert len(requests) == (1 if no_record else 2)
    if not no_record:
        assert "Warning" in result.stderr


def test_denial_receipt_recipe(transport):
    requests, replies = transport
    replies.extend(
        [httpx.Response(200, json=DENIED), httpx.Response(202, json={"ingested": 1})]
    )
    result = runner.invoke(
        app,
        [
            "--json",
            *CHECK,
            "--tag",
            "env=prod",
            "--estimated-output-tokens",
            "30",
            "--agent-run-id",
            "job",
        ],
    )
    assert result.exit_code == 4, result.output
    data = json.loads(result.stdout)
    event = requests[1][2]["json"][0]
    assert data["recorded"]["status"] == "ingested"
    assert event["call_id"] == data["call_id"]
    assert event["status"] == "budget_denied"
    assert event["input_tokens"] == 21 and event["output_tokens"] == 0
    assert event["latency_ms"] == 0 and event["estimated_output_bound"] == 30
    assert event["deny_source"] == "server"
    assert event["deny_reason"] == event["denied_by_period"] == "tag"
    assert event["agent_run_id"] == "job" and event["tags"] == {"env": "prod"}
    assert "receipt_aggregate_count" not in event and "lease_id" not in event


def test_standalone_record_local_provider(transport):
    requests, replies = transport
    replies.append(httpx.Response(202, json={"ingested": 1}))
    result = runner.invoke(
        app,
        [
            "--json",
            "record",
            "--model",
            "llama3",
            "--provider",
            "ollama",
            "--call-id",
            CALL_ID,
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["schema"] == "solwyn.record.v1"
    assert requests[0][2]["json"][0]["provider"] == "ollama"


def test_batch_chunks_share_instance_and_correlate_indexes(transport, tmp_path):
    requests, replies = transport
    events = [{"model": "llama3", "provider": "ollama"} for _ in range(1001)]
    events[-1]["call_id"] = CALL_ID
    source = tmp_path / "batch.json"
    source.write_text(json.dumps(events))
    replies.extend(
        [
            httpx.Response(202, json={"ingested": 1000}),
            httpx.Response(
                202,
                json={
                    "ingested": 0,
                    "duplicates": [
                        {"index": 0, "reason": "legacy_key", "call_id": CALL_ID}
                    ],
                },
            ),
        ]
    )
    result = runner.invoke(app, ["--json", "record", "--batch", str(source)])
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert data["duplicates"][0]["index"] == 1000
    bodies = [event for request in requests for event in request[2]["json"]]
    assert len({event["sdk_instance_id"] for event in bodies}) == 1
    assert len({event["timestamp"] for event in bodies}) == 1001


@pytest.mark.parametrize("extra", ["lease_id", "cost_usd"])
def test_batch_rejects_unadopted_input_before_mutation(transport, extra):
    requests, _ = transport
    result = runner.invoke(
        app,
        ["--json", "record", "--batch", "-"],
        input=json.dumps([{"model": "llama3", "provider": "ollama", extra: "bad"}]),
    )
    assert result.exit_code == 2
    assert requests == []


@pytest.mark.parametrize("mode", ["table", "json"])
@pytest.mark.parametrize("case", ["single", "batch", "rejection", "dedup", "legacy"])
def test_record_output_goldens(transport, mode, case):
    requests, replies = transport
    body = {"ingested": 1}
    if case == "batch":
        body = {"ingested": 2}
    if case == "rejection":
        body = {
            "ingested": 0,
            "rejected": [
                {
                    "index": 0,
                    "code": "unknown_model",
                    "model": "llama3",
                    "message": "Use the provider model name verbatim.",
                }
            ],
        }
    if case in ("dedup", "legacy"):
        body = {
            "ingested": 0,
            "duplicates": [
                {
                    "index": 0,
                    "reason": "call_id" if case == "dedup" else "legacy_key",
                    "call_id": CALL_ID,
                }
            ],
        }
    replies.append(httpx.Response(202, json=body))
    options = ["--json"] if mode == "json" else ["--no-color"]
    args = ["record", "--provider", "ollama", "--model", "llama3", "--call-id", CALL_ID]
    input_text = None
    if case == "batch":
        args = ["record", "--batch", "-"]
        input_text = json.dumps(
            [
                {"model": "llama3", "provider": "ollama", "call_id": CALL_ID},
                {
                    "model": "llama3",
                    "provider": "ollama",
                    "call_id": "12345678-1234-1234-1234-123456789abd",
                },
            ]
        )
    result = runner.invoke(app, [*options, *args], input=input_text)
    assert result.exit_code == (1 if case in ("rejection", "legacy") else 0), (
        result.output
    )
    suffix = "json" if mode == "json" else "txt"
    assert_matches_golden(result.stdout, GOLDEN_DIR / f"record-{case}-{mode}.{suffix}")


@pytest.mark.parametrize("call_id", ["call-id", "12345678-1234-1234-1234-123456789ABC"])
def test_confirm_canonical_uuid_before_settlement(transport, call_id):
    requests, _ = transport
    args = [call_id if value == CALL_ID else value for value in CONFIRM]
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 2
    assert "uuidgen" in result.stderr
    assert requests == []


@pytest.mark.parametrize(
    "status_code,detail,expected",
    [
        (403, {"code": "read_only_key", "message": "This key is read-only"}, 3),
        (400, "Batch too large (max 1000 events)", 2),
    ],
)
def test_record_transport_error_contract(transport, status_code, detail, expected):
    _, replies = transport
    replies.append(
        error_from_response(httpx.Response(status_code, json={"detail": detail}))
    )
    result = runner.invoke(
        app, ["--json", "record", "--model", "llama3", "--provider", "ollama"]
    )
    assert result.exit_code == expected
    assert result.stdout == ""
    if status_code == 403:
        assert "full-scope" in result.stderr


@pytest.mark.parametrize("no_record", [False, True])
def test_allowed_and_fail_open_never_record(transport, no_record):
    requests, replies = transport
    replies.append(
        httpx.Response(200, json={**DENIED, "allowed": True, "denied_by_period": None})
    )
    result = runner.invoke(
        app, ["--json", *CHECK, *(["--no-record"] if no_record else [])]
    )
    assert result.exit_code == 0
    assert len(requests) == 1
    assert "recorded" not in json.loads(result.stdout)


def test_denial_no_record_retains_exit4(transport):
    requests, replies = transport
    replies.append(httpx.Response(200, json=DENIED))
    result = runner.invoke(app, ["--json", *CHECK, "--no-record"])
    assert result.exit_code == 4
    assert len(requests) == 1
    assert json.loads(result.stdout)["recorded"]["status"] == "skipped"


def test_confirm_rejection_remains_successful_with_warning(transport):
    _, replies = transport
    replies.extend(
        [
            httpx.Response(204),
            httpx.Response(
                202,
                json={
                    "ingested": 0,
                    "rejected": [
                        {
                            "index": 0,
                            "code": "unknown_model",
                            "model": "gpt-4o",
                            "message": "Specific Core guidance.",
                        }
                    ],
                },
            ),
        ]
    )
    result = runner.invoke(app, ["--json", *CONFIRM])
    assert result.exit_code == 0
    assert "Use the exact model name." in result.stderr and "Warning" in result.stderr
    assert "Specific Core guidance." not in result.stdout + result.stderr
    assert json.loads(result.stdout)["recorded"]["status"] == "rejected"


@pytest.mark.parametrize("mode", ["table", "json"])
def test_tag_denial_goldens(transport, monkeypatch, mode):
    _, replies = transport
    monkeypatch.setattr(budget, "uuid4", lambda: CALL_ID)
    replies.extend(
        [httpx.Response(200, json=DENIED), httpx.Response(202, json={"ingested": 1})]
    )
    result = runner.invoke(
        app,
        [
            *(["--json"] if mode == "json" else ["--no-color"]),
            *CHECK,
            "--tag",
            "env=prod",
            "--estimated-output-tokens",
            "30",
        ],
    )
    assert result.exit_code == 4
    assert "denied by tag cap `env`" in result.stderr
    suffix = "json" if mode == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"budget-check-tag-deny-{mode}.{suffix}"
    )


def test_environment_run_id_and_tags_reach_confirm_ingest(transport, monkeypatch):
    requests, replies = transport
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "job-env")
    monkeypatch.setenv("SOLWYN_TAGS", "env=prod")
    replies.extend([httpx.Response(204), httpx.Response(202, json={"ingested": 1})])
    result = runner.invoke(app, ["--json", *CONFIRM])
    assert result.exit_code == 0
    assert requests[1][2]["json"][0]["agent_run_id"] == "job-env"
    assert requests[1][2]["json"][0]["tags"] == {"env": "prod"}


def test_multiple_tags_do_not_invent_a_denying_key(transport):
    _, replies = transport
    replies.extend(
        [httpx.Response(200, json=DENIED), httpx.Response(202, json={"ingested": 1})]
    )
    result = runner.invoke(
        app, ["--json", *CHECK, "--tag", "env=prod", "--tag", "team=ops"]
    )
    assert result.exit_code == 4
    assert result.stderr == "denied by tag cap\n"


def test_uncertain_record_failure_retains_generated_call_ids_for_safe_retry(transport):
    requests, replies = transport
    replies.append(
        ApiError(
            status_code=None,
            code="transport_error",
            message="Could not connect to the Solwyn API.",
            hint=None,
            detail=None,
        )
    )
    result = runner.invoke(
        app, ["--json", "record", "--model", "llama3", "--provider", "ollama"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["call_ids"] == [requests[0][2]["json"][0]["call_id"]]
    assert "transport_error" in result.stderr
