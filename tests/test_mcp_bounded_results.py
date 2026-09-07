"""Complete MCP wire size and known mutation/partial-read state at limits."""

import threading
import time

import anyio
import httpx
import pytest

from solwyn_cli import auth, _mcp_operation as operation
from solwyn_cli.commands import budget, _record
from solwyn_cli.mcp import server
from test_mcp_conformance import VALID_KEY, CHECK_RESPONSE, _call_tool, _install_http
from test_alerts_command import EVENT, WINDOW
from test_budget_batch import CHECK
from mcp_batch_fixtures import ID, journal

KEY = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
CALL_ID = "12345678-1234-4000-8000-123456789abc"


def install(monkeypatch, responses):
    return _install_http(monkeypatch, responses=responses, credentials=[KEY])


def test_unicode_escaped_mutation_output_cap_keeps_known_reservation(monkeypatch):
    payload = {**CHECK_RESPONSE, "project_id": "🧪" * 200_000}
    assert len(httpx.Response(200, json=payload).content) < operation.MAX_RESPONSE_BYTES
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=payload)])
    result = anyio.run(_call_tool, "budget_check", CHECK)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "result_size_limit"
    assert result.structured_content["data"] is None
    assert (
        result.structured_content["recovery"]["reservation_id"]
        == CHECK_RESPONSE["reservation_id"]
    )
    assert (
        len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
        <= operation.MAX_RESULT_BYTES
    )
    assert len(requests) == 1


def test_read_output_limit_returns_original_cursor_when_no_rows_can_be_delivered(
    monkeypatch,
):
    payload = {
        "events": [{**EVENT, "metadata_json": {"large": "🧪" * 200_000}}],
        "next_cursor": "successor",
        "window": WINDOW,
    }
    assert len(httpx.Response(200, json=payload).content) < operation.MAX_RESPONSE_BYTES
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=payload)])
    result = anyio.run(_call_tool, "list_alert_events", {"cursor": "original"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "result_size_limit"
    assert result.structured_content["recovery"]["cursor"] == "original"
    assert result.structured_content["data"] is None
    assert len(requests) == 1


def test_page_limit_delivers_completed_pages_and_exact_unread_cursor(monkeypatch):
    responses = [
        httpx.Response(
            200,
            json={
                "events": [{**EVENT, "id": f"event-{i}"}],
                "next_cursor": f"page-{i + 1}",
                "window": WINDOW,
            },
        )
        for i in range(10)
    ]
    requests, _, _ = install(monkeypatch, responses)
    result = anyio.run(_call_tool, "list_alert_events", {"all_pages": True})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "page_limit"
    data = result.structured_content["data"]
    assert [entry["id"] for entry in data["events"]] == [
        f"event-{i}" for i in range(10)
    ]
    assert (
        data["next_cursor"]
        == result.structured_content["recovery"]["cursor"]
        == "page-10"
    )
    assert len(requests) == 10


@pytest.mark.parametrize("tool", ["budget_confirm", "budget_check_batch"])
def test_deadline_after_admission_keeps_known_state_and_blocks_recording(
    monkeypatch, tool
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
    if tool == "budget_confirm":
        responses = [httpx.Response(204)]
        args = {
            "reservation_id": "known",
            "call_id": CALL_ID,
            "model": "gpt-4o",
            "provider": "openai",
        }
    else:
        denied = {**CHECK_RESPONSE, "allowed": False, "reservation_id": None}
        responses = [httpx.Response(200, json=journal([CHECK_RESPONSE, denied]))]
        args = {"operation_id": ID, "checks": [CHECK, CHECK]}
    requests, _, _ = install(monkeypatch, responses)
    result = anyio.run(_call_tool, tool, args)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "operation_deadline"
    recovery = result.structured_content["recovery"]
    if tool == "budget_confirm":
        assert recovery["settlement"] == "confirmed"
        assert recovery["call_id"] == CALL_ID
        assert result.structured_content["data"]["reservation_id"] == "known"
    else:
        assert recovery["state"] == "finished"
        assert (
            result.structured_content["data"]["items"][0]["result"]["reservation_id"]
            == CHECK_RESPONSE["reservation_id"]
        )
        assert result.structured_content["data"]["items"][1]["recording_retry"][
            "event"
        ]["call_id"]
    assert finished.wait(1)
    assert len(requests) == 1


def test_output_validation_and_control_work_share_the_initial_deadline(monkeypatch):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.04)

    def blocked(launch):
        time.sleep(0.1)
        operation.checkpoint()
        pytest.fail("deadline must stop further work")

    monkeypatch.setattr(server, "_overview_operation", blocked)
    result = anyio.run(_call_tool, "get_overview", {})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "operation_deadline"


def test_admitted_denial_recording_timeout_is_unknown_in_each_retained_view(
    monkeypatch,
):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.06)
    completed = threading.Event()

    class SlowReceipt(httpx.SyncByteStream):
        def __iter__(self):
            time.sleep(0.12)
            completed.set()
            yield b'{"ingested": 1}'

    denied = {**CHECK_RESPONSE, "allowed": False, "reservation_id": None}
    requests, _, _ = install(
        monkeypatch,
        [
            httpx.Response(200, json=journal([CHECK_RESPONSE, denied])),
            httpx.Response(202, stream=SlowReceipt()),
        ],
    )
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK, CHECK]}
    )
    assert result.structured_content["error"]["code"] == "operation_deadline"
    payload = result.structured_content
    assert payload["recovery"]["recording"] == "unknown"
    assert payload["recovery"]["items"][1]["recording_status"] == "unknown"
    assert payload["data"]["items"][1]["result"]["recorded"]["status"] == "unknown"
    assert (
        payload["data"]["items"][0]["result"]["reservation_id"]
        == CHECK_RESPONSE["reservation_id"]
    )
    assert completed.wait(1)
    assert len(requests) == 2


def test_tag_values_oversized_output_preserves_its_input_cursor(monkeypatch):
    payload = {
        "key": "task",
        "values": [
            {
                "value": "🧪" * 200_000,
                "first_seen_at": "2026-09-06T00:00:00Z",
                "last_seen_at": "2026-09-06T00:00:00Z",
            }
        ],
        "next_cursor": "next",
        "has_more": True,
    }
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=payload)])
    result = anyio.run(
        _call_tool, "list_tag_values", {"key": "task", "cursor": "original"}
    )
    assert result.structured_content["error"]["code"] == "result_size_limit"
    assert result.structured_content["recovery"]["cursor"] == "original"
    assert len(requests) == 1


@pytest.mark.parametrize("tool", ["budget_check", "budget_check_batch"])
def test_deadline_after_atomic_retention_preserves_compact_known_reservation(
    monkeypatch, tool
):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.08)
    original = operation.retain
    entered = threading.Event()
    finished = threading.Event()

    def suspend_after_known_data(data, **recovery):
        original(data, **recovery)
        entered.set()
        time.sleep(0.20)
        finished.set()

    monkeypatch.setattr(operation, "retain", suspend_after_known_data)
    decision = {**CHECK_RESPONSE, "project_id": "🧪" * 200_000}
    body = journal([decision]) if tool == "budget_check_batch" else decision
    assert len(httpx.Response(200, json=body).content) < operation.MAX_RESPONSE_BYTES
    requests, _, _ = install(monkeypatch, [httpx.Response(200, json=body)])
    arguments = (
        {"operation_id": ID, "checks": [CHECK], "no_record": True}
        if tool == "budget_check_batch"
        else CHECK
    )
    result = anyio.run(_call_tool, tool, arguments)
    assert entered.is_set()
    assert finished.wait(1)
    assert len(requests) == 1
    payload = result.structured_content
    assert result.is_error
    assert payload["data"] is None
    assert payload["error"]["code"] == "result_size_limit"
    assert (
        len(result.model_dump_json(by_alias=True).encode())
        <= operation.MAX_RESULT_BYTES
    )
    recovery = payload.get("recovery") or {}
    if tool == "budget_check_batch":
        assert recovery["operation_id"] == ID
        assert recovery["state"] == "finished"
        assert [item["state"] for item in recovery["items"]] == ["completed"]
        assert [item["reservation_id"] for item in recovery["items"]] == [
            CHECK_RESPONSE["reservation_id"]
        ]
    else:
        assert recovery.get("reservation_id") == CHECK_RESPONSE["reservation_id"]


@pytest.mark.parametrize("oversized", [False, True])
def test_error_serialization_uses_one_snapshot_even_if_worker_publishes_again(
    monkeypatch, oversized
):
    first = budget.BudgetCheckData.model_validate(
        {**CHECK_RESPONSE, "project_id": "🧪" * 200_000 if oversized else "first"}
    )
    later = budget.BudgetCheckData.model_validate(
        {**CHECK_RESPONSE, "reservation_id": "later-reservation", "project_id": "later"}
    )
    calls = []
    original = operation.OperationControl.snapshot

    def capture_then_publish(self):
        captured = original(self)
        calls.append(captured)
        # Simulate an abandoned worker finishing local validation after the
        # serializer captures its chosen generation, without admitting HTTP.
        self.retain(later, reservation_id=later.reservation_id)
        return captured

    monkeypatch.setattr(operation.OperationControl, "snapshot", capture_then_publish)

    async def exercise():
        async with operation.operation_scope() as control:
            control.retain(first, reservation_id=first.reservation_id)
            result = server._bounded_protocol_result(
                server._error(
                    server._BudgetCheckResult,
                    code="operation_deadline",
                    message="Fixed test failure.",
                ),
                control,
            )
            payload = result.structured_content
            assert payload["recovery"]["reservation_id"] == first.reservation_id
            if oversized:
                assert payload["data"] is None
                assert payload["error"]["code"] == "result_size_limit"
            else:
                assert payload["data"]["reservation_id"] == first.reservation_id
                assert payload["data"]["project_id"] == "first"
            assert len(calls) == 1

    anyio.run(exercise)
