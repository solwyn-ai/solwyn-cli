"""Stable caller-owned batch identity, conservative outcomes and GET-only lookup."""

from datetime import UTC, datetime
from uuid import UUID, uuid5

import anyio
import httpx
import pytest

from solwyn_cli import auth
from test_mcp_conformance import VALID_KEY, _call_tool, _install_http
from test_budget_batch import CHECK, ALLOWED, DENIED

from mcp_batch_fixtures import ID, CHECKED, journal


def install(monkeypatch, responses):
    monkeypatch.delenv("SOLWYN_TAGS", raising=False)
    monkeypatch.delenv("SOLWYN_AGENT_RUN_ID", raising=False)
    return _install_http(
        monkeypatch,
        responses=responses,
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
        ],
    )


def test_post_is_one_identity_bound_attempt_and_recovery_is_get_only(monkeypatch):
    requests, _, _ = install(
        monkeypatch, [httpx.Response(200, json=journal([ALLOWED]))]
    )
    result = anyio.run(
        _call_tool,
        "budget_check_batch",
        {"operation_id": ID, "checks": [CHECK], "no_record": True},
    )
    assert not result.is_error
    assert result.structured_content["data"]["operation_id"] == ID
    assert requests[0][1] == "/api/v1/budgets/check/batches"
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=journal([DENIED]))])
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "recover_only": True}
    )
    assert not result.is_error
    assert [(r[0], r[1]) for r in requests] == [
        ("GET", f"/api/v1/budgets/check/batches/{ID}")
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"checks": [CHECK] * 33},
        {"recover_only": True},
        {"operation_id": ID, "checks": [CHECK], "recover_only": True},
        {"operation_id": "00000000-0000-4000-8000-000000000001", "checks": [CHECK]},
    ],
)
def test_invalid_batch_modes_fail_before_auth(monkeypatch, args):
    def fail():
        pytest.fail("auth must not run")

    monkeypatch.setattr(auth, "resolve_credential", fail)
    result = anyio.run(_call_tool, "budget_check_batch", args)
    assert result.is_error


def test_lost_post_retains_identity_and_never_replays(monkeypatch):
    requests, _, _ = install(monkeypatch, [httpx.ReadError("PRIVATE")])
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK]}
    )
    assert result.is_error
    assert result.structured_content["recovery"]["operation_id"] == ID
    assert result.structured_content["recovery"]["state"] == "unknown"
    assert len(requests) == 1
    assert "PRIVATE" not in result.model_dump_json()


def test_denial_records_retain_frozen_payload_and_stable_identity(monkeypatch):
    requests, _, _ = install(
        monkeypatch,
        [
            httpx.Response(200, json=journal([ALLOWED, DENIED])),
            httpx.Response(202, json={"ingested": 1}),
        ],
    )
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK, CHECK]}
    )
    assert not result.is_error
    receipt = result.structured_content["data"]["items"][1]["recording_retry"]
    event = requests[1][2]["json"][0]
    assert event["call_id"] == str(uuid5(UUID(ID), "denial:1"))
    assert event["sdk_instance_id"] == ID
    assert datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")) == CHECKED
    requests, _, _ = install(
        monkeypatch,
        [
            httpx.Response(
                202,
                json={
                    "ingested": 0,
                    "duplicates": [
                        {"index": 0, "reason": "call_id", "call_id": event["call_id"]}
                    ],
                },
            )
        ],
    )
    retried = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "record_only": [receipt]}
    )
    assert not retried.is_error
    assert len(requests) == 1
    assert requests[0][1] == "/api/v1/metadata/ingest"
    assert requests[0][2]["json"][0] == event


@pytest.mark.parametrize("status", [401, 429])
def test_reservation_post_never_automatically_replays_auth_or_rate_response(
    monkeypatch, status
):
    requests, _, remaining = install(
        monkeypatch,
        [
            httpx.Response(
                status, headers={"Retry-After": "0"}, json={"detail": "rejected"}
            )
        ],
    )
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK]}
    )
    assert result.is_error
    assert len(requests) == 1
    assert not remaining
    assert result.structured_content["recovery"]["operation_id"] == ID


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc["items"].reverse(),
        lambda doc: doc["items"][1].update(index=0),
        lambda doc: doc["items"][0].update(state="unknown"),
        lambda doc: doc["items"][0].update(reservation_expires_at=CHECKED.isoformat()),
        lambda doc: doc.update(expires_at=CHECKED.isoformat()),
        lambda doc: doc["items"][0].update(checked_at=None),
        lambda doc: doc["items"][0]["result"].update(call_id="untrusted-receipt"),
        lambda doc: doc["items"][0]["result"].update(
            recorded={"status": "ingested", "ingested": 1, "call_ids": []}
        ),
    ],
)
def test_inconsistent_journal_rejected_without_any_denial_ingest(monkeypatch, mutation):
    document = journal([ALLOWED, DENIED])
    mutation(document)
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=document)])
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK, CHECK]}
    )
    assert result.is_error
    assert len(requests) == 1
    assert result.structured_content["recovery"]["state"] == "unknown"


def test_unknown_failed_unattempted_lanes_remain_honest(monkeypatch):
    for state, code in (("unknown", "reservation_unknown"), ("failed", "check_failed")):
        document = journal([ALLOWED, ALLOWED, ALLOWED], status="interrupted")
        document["items"][1].update(
            state=state, result=None, error_code=code, reservation_expires_at=None
        )
        document["items"][2].update(
            state="unattempted",
            result=None,
            error_code=None,
            checked_at=None,
            reservation_expires_at=None,
        )
        requests, _, _ = install(monkeypatch, [httpx.Response(200, json=document)])
        result = anyio.run(
            _call_tool, "budget_check_batch", {"operation_id": ID, "recover_only": True}
        )
        assert result.is_error
        items = result.structured_content["data"]["items"]
        assert [item["state"] for item in items] == ["completed", state, "unattempted"]
        assert items[0]["result"]["reservation_id"] == ALLOWED["reservation_id"]
        assert len(requests) == 1


def test_generated_uuid7_identity_exists_even_when_auth_is_missing(monkeypatch):
    install(monkeypatch, [])
    monkeypatch.setattr(auth, "resolve_credential", lambda: None)
    result = anyio.run(_call_tool, "budget_check_batch", {"checks": [CHECK]})
    identity = UUID(result.structured_content["recovery"]["operation_id"])
    assert identity.version == 7
    assert abs(datetime.now(UTC).timestamp() * 1000 - (identity.int >> 80)) < 5000
    assert result.structured_content["recovery"]["state"] == "not_submitted"


def test_record_only_legacy_collision_is_unknown_even_when_reply_echoes_call_id(
    monkeypatch,
    capsys,
):
    # Core's legacy lane can acknowledge the submitted ID when a different call
    # owns (timestamp, sdk_instance_id). Echoing the ID is not a ledger receipt.
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=journal([DENIED]))])
    first = anyio.run(
        _call_tool,
        "budget_check_batch",
        {"operation_id": ID, "checks": [CHECK], "no_record": True},
    )
    receipt = first.structured_content["data"]["items"][0]["recording_retry"]
    call_id = receipt["event"]["call_id"]
    requests, _, _ = install(
        monkeypatch,
        [
            httpx.Response(
                202,
                json={
                    "ingested": 0,
                    "duplicates": [
                        {"index": 0, "reason": "legacy_key", "call_id": call_id}
                    ],
                },
            )
        ],
    )
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "record_only": [receipt]}
    )
    assert result.is_error
    assert result.structured_content["data"]["recordings"][0]["status"] == "unknown"
    assert result.structured_content["recovery"]["call_id"] == call_id
    assert result.structured_content["recovery"]["recording"] == "unknown"
    assert result.structured_content["error"]["code"] == "recording_unknown"
    assert len(requests) == 1 and requests[0][1] == "/api/v1/metadata/ingest"
    assert "attribution was not recorded" not in capsys.readouterr().err.lower()
