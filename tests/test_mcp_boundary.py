"""Execution errors, strict input intent, and privacy at the MCP wire boundary."""

import asyncio
import json
import logging
from typing import ClassVar

import anyio
import httpx
import pytest
from mcp import Client, MCPError, types
from mcp_batch_fixtures import ID, journal
from pydantic import BaseModel
from test_mcp_conformance import (
    CHECK_RESPONSE,
    VALID_KEY,
    _call_tool,
    _install_http,
    _listed_tool_contract,
)

from solwyn_cli import auth
from solwyn_cli.commands import _record, budget
from solwyn_cli.mcp import server as mcp_server

CANARY = "SYNTHETIC_PRIVATE_BOUNDARY_91a5"
CHECK = {"estimated_input_tokens": 12, "model": "gpt-4o", "provider": "openai"}
CALL_ID = "12345678-1234-1234-1234-123456789abc"
CONFIRM = {"reservation_id": "res_known", "call_id": CALL_ID, **CHECK}
CONFIRM.pop("estimated_input_tokens")


def _key():
    return auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")


def _assert_private(result, caplog, capsys):
    # Check all representations independently, including captured stderr and log args.
    assert CANARY not in result.model_dump_json(by_alias=True)
    assert CANARY not in json.dumps(result.structured_content)
    assert CANARY not in " ".join(item.text for item in result.content)
    assert json.loads(result.content[0].text) == result.structured_content
    assert CANARY not in caplog.text
    assert CANARY not in repr([(r.msg, r.args, r.exc_info) for r in caplog.records])
    captured = capsys.readouterr()
    assert CANARY not in captured.err + captured.out


@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        ("budget_status", {"projectid": CANARY}, "arguments"),
        ("budget_check", {**CHECK, "noRecord": True}, "arguments"),
        ("budget_check", {**CHECK, CANARY: CANARY}, "arguments"),
        (
            "budget_check",
            {**CHECK, "estimated_input_tokens": CANARY},
            "estimated_input_tokens",
        ),
        ("budget_check", {**CHECK, "provider": CANARY}, "provider"),
        ("budget_check", {**CHECK, "tags": {CANARY: {"secret": CANARY}}}, "tags"),
        (
            "budget_check_batch",
            {"checks": [{**CHECK, "estimated_media": {"audioSecond": CANARY}}]},
            "checks[].estimated_media",
        ),
        (
            "budget_check_batch",
            {"checks": [{**CHECK, "estimated_media": {"audio_seconds": CANARY}}]},
            "checks[].estimated_media.audio_seconds",
        ),
        ("budget_check_batch", {"checks": [{**CHECK, CANARY: CANARY}]}, "checks[]"),
        (
            "record_call",
            {
                "model": "gpt-4o",
                "provider": "openai",
                "token_details": {"inputTokens": CANARY},
            },
            "token_details",
        ),
        (
            "record_call",
            {
                "model": "gpt-4o",
                "provider": "openai",
                "media_usage": {"audio_seconds": CANARY},
            },
            "media_usage.audio_seconds",
        ),
    ],
)
def test_invalid_arguments_are_private_and_stop_before_auth(
    monkeypatch, caplog, capsys, tool, arguments, path
):
    calls = []

    def unexpected_auth(**_: object):
        calls.append("auth")
        raise RuntimeError(CANARY)

    monkeypatch.setattr(auth, "resolve_credential", unexpected_auth)
    result = anyio.run(_call_tool, tool, arguments)
    _assert_private(result, caplog, capsys)
    assert calls == []
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "invalid_arguments"
    assert path in result.structured_content["error"]["hint"]


def test_published_inputs_are_closed_and_tag_keys_remain_open():
    catalog = anyio.run(_listed_tool_contract)
    for tool in catalog:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        for definition in schema.get("$defs", {}).values():
            if definition.get("type") == "object":
                assert definition["additionalProperties"] is False
    check = next(tool for tool in catalog if tool["name"] == "budget_check")
    assert check["inputSchema"]["properties"]["tags"]["anyOf"][0][
        "additionalProperties"
    ] == {"type": "string"}


def test_unexpected_exception_is_private_in_wire_and_logs(monkeypatch, caplog, capsys):
    def unexpected(*args, **kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(mcp_server, "_budget_status_operation", unexpected)
    caplog.set_level(logging.DEBUG, logger="mcp.server.lowlevel.server")
    result = anyio.run(_call_tool, "budget_status", {})
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "internal_error"
    assert result.structured_content["schema"] == "solwyn.mcp.budget-status.v1"


def test_validation_canary_is_private_with_sdk_debug_logging(
    monkeypatch, caplog, capsys
):
    monkeypatch.setattr(auth, "resolve_credential", lambda **_: None)
    caplog.set_level(logging.DEBUG, logger="mcp.server.lowlevel.server")
    result = anyio.run(
        _call_tool, "budget_check", {**CHECK, "estimated_input_tokens": CANARY}
    )
    _assert_private(result, caplog, capsys)
    assert result.is_error is True


def test_unknown_tool_name_is_not_reflected(monkeypatch, caplog, capsys):
    monkeypatch.setattr(auth, "resolve_credential", lambda **_: None)

    async def exercise():
        async with Client(mcp_server.create_server(), cache=None) as client:
            with pytest.raises(MCPError) as raised:
                await client.call_tool(CANARY, {})
            assert raised.value.code == -32602
            assert str(raised.value) == "Unknown tool"

    anyio.run(exercise)
    assert CANARY not in caplog.text
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err


def test_missing_auth_sets_protocol_error(monkeypatch):
    requests, _, _ = _install_http(monkeypatch, responses=[], credentials=[None])
    result = anyio.run(_call_tool, "budget_status", {})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "authentication_required"
    assert requests == []


@pytest.mark.parametrize(
    "tool,arguments,code",
    [
        ("get_costs", {"from_": CANARY}, "invalid_cost_query"),
        ("list_alert_events", {"from_": CANARY}, "invalid_alert_query"),
        ("list_agent_runs", {"from_time": CANARY}, "custom_window_pair_required"),
    ],
)
def test_query_errors_are_private_execution_errors(
    monkeypatch, caplog, capsys, tool, arguments, code
):
    requests, _, _ = _install_http(monkeypatch, responses=[], credentials=[_key()])
    result = anyio.run(_call_tool, tool, arguments)
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == code
    assert requests == []


@pytest.mark.parametrize(
    "status,code",
    [(402, "upgrade_required"), (403, "forbidden"), (429, "rate_limited")],
)
def test_api_failures_set_protocol_error(monkeypatch, caplog, capsys, status, code):
    responses = [
        httpx.Response(status, json={"detail": {"code": code, "message": CANARY}})
    ]
    if status == 429:
        responses *= 2
    requests, _, remaining = _install_http(
        monkeypatch, responses=responses, credentials=[_key()]
    )
    result = anyio.run(_call_tool, "get_costs", {})
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == code
    assert result.structured_content["error"]["status_code"] == status
    assert remaining == []
    assert len(requests) == (2 if status == 429 else 1)


def test_transport_failure_sets_protocol_error(monkeypatch, caplog, capsys):
    def disconnected(request):
        raise httpx.ConnectError(CANARY, request=request)

    _install_http(monkeypatch, responses=[], credentials=[_key()])
    monkeypatch.setattr(
        httpx.MockTransport,
        "handle_request",
        lambda self, request: disconnected(request),
    )
    result = anyio.run(_call_tool, "budget_status", {})
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "api_error"
    assert result.structured_content["error"]["status_code"] is None


@pytest.mark.parametrize("allowed", [False, True])
def test_denial_and_fail_open_are_successful_decisions(monkeypatch, allowed):
    response = {**CHECK_RESPONSE, "allowed": allowed, "reservation_id": None}
    requests, _, remaining = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=response)],
        credentials=[_key()],
    )
    result = anyio.run(
        _call_tool,
        "budget_check",
        {**CHECK, "no_record": True, "tags": {"caller-defined": "ok"}},
    )
    assert result.is_error is False
    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["allowed"] is allowed
    assert result.structured_content["data"]["reservation_id"] is None
    if not allowed:
        assert result.structured_content["data"]["recorded"]["status"] == "skipped"
    assert len(requests) == 1
    assert requests[0][2]["json"]["tags"] == {"caller-defined": "ok"}
    assert remaining == []


def test_later_batch_failure_retains_completed_reservations(monkeypatch):
    body = journal([CHECK_RESPONSE] * 3, status="interrupted")
    body["items"][1].update(
        state="unknown",
        result=None,
        error_code="reservation_unknown",
        reservation_expires_at=None,
    )
    body["items"][2].update(
        state="unattempted",
        result=None,
        error_code=None,
        checked_at=None,
        reservation_expires_at=None,
    )
    requests, _, remaining = _install_http(
        monkeypatch, responses=[httpx.Response(200, json=body)], credentials=[_key()]
    )
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK] * 3}
    )
    assert result.is_error is True
    data = result.structured_content["data"]
    assert [item["state"] for item in data["items"]] == [
        "completed",
        "unknown",
        "unattempted",
    ]
    assert (
        data["results"][0]["result"]["reservation_id"]
        == CHECK_RESPONSE["reservation_id"]
    )
    assert len(requests) == 1 and remaining == []


def test_batch_item_error_is_private_and_retains_recovery_identity(
    monkeypatch, caplog, capsys
):
    body = journal([CHECK_RESPONSE] * 2, status="interrupted")
    body["items"][1].update(
        state="failed", result=None, error_code=CANARY, reservation_expires_at=None
    )
    requests, _, _ = _install_http(
        monkeypatch, responses=[httpx.Response(200, json=body)], credentials=[_key()]
    )
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK, CHECK]}
    )
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    # A malformed journal is untrusted as a whole. Do not manufacture known
    # admission from invalid server data; the caller can perform GET recovery.
    assert result.structured_content["data"] is None
    assert result.structured_content["recovery"]["operation_id"] == ID
    assert result.structured_content["recovery"]["state"] == "unknown"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body,status",
    [
        ({"ingested": 1}, "ingested"),
        (
            {
                "ingested": 0,
                "duplicates": [{"index": 0, "reason": "call_id", "call_id": CALL_ID}],
            },
            "deduplicated",
        ),
        (
            {
                "ingested": 0,
                "rejected": [
                    {
                        "index": 0,
                        "code": "unknown_model",
                        "model": CANARY,
                        "message": CANARY,
                    }
                ],
            },
            "rejected",
        ),
    ],
)
def test_record_disposition_and_rejection_privacy(
    monkeypatch, caplog, capsys, body, status
):
    requests, _, _ = _install_http(
        monkeypatch, responses=[httpx.Response(202, json=body)], credentials=[_key()]
    )
    result = anyio.run(
        _call_tool,
        "record_call",
        {"model": "gpt-4o", "provider": "openai", "call_id": CALL_ID},
    )
    _assert_private(result, caplog, capsys)
    assert result.is_error is (status == "rejected")
    assert result.structured_content["ok"] is (status != "rejected")
    assert result.structured_content["data"]["status"] == status
    assert result.structured_content["data"]["call_ids"] == [CALL_ID]
    assert len(requests) == 1


@pytest.mark.parametrize("no_record,status", [(False, "ingested"), (True, "skipped")])
def test_successful_settlement_preserves_recording_state(
    monkeypatch, no_record, status
):
    responses = [httpx.Response(204)] + (
        [] if no_record else [httpx.Response(202, json={"ingested": 1})]
    )
    requests, _, _ = _install_http(
        monkeypatch, responses=responses, credentials=[_key()]
    )
    result = anyio.run(
        _call_tool, "budget_confirm", {**CONFIRM, "no_record": no_record}
    )
    assert result.is_error is False
    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["reservation_id"] == "res_known"
    assert result.structured_content["data"]["call_id"] == CALL_ID
    assert result.structured_content["data"]["recorded"]["status"] == status
    assert len(requests) == (1 if no_record else 2)


def test_settlement_survives_private_recording_validation_failure(
    monkeypatch, caplog, capsys
):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(204), httpx.Response(202, json={"ingested": CANARY})],
        credentials=[_key()],
    )
    result = anyio.run(_call_tool, "budget_confirm", CONFIRM)
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "recording_unknown"
    assert "settlement succeeded" in result.structured_content["error"]["message"]
    assert result.structured_content["data"]["reservation_id"] == "res_known"
    assert result.structured_content["data"]["recorded"]["status"] == "unknown"
    assert result.structured_content["data"]["recorded"]["call_ids"] == [CALL_ID]
    assert [request[1] for request in requests] == [
        "/api/v1/budgets/confirm",
        "/api/v1/metadata/ingest",
    ]


@pytest.mark.parametrize("failure", [ValueError, RuntimeError])
def test_denial_recording_error_is_private_and_keeps_valid_decision(
    monkeypatch, caplog, capsys, failure
):
    def invalid_event(**kwargs):
        raise failure(CANARY)

    monkeypatch.setattr(budget, "build_event", invalid_event)
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                200, json={**CHECK_RESPONSE, "allowed": False, "reservation_id": None}
            )
        ],
        credentials=[_key()],
    )
    result = anyio.run(_call_tool, "budget_check", CHECK)
    _assert_private(result, caplog, capsys)
    assert result.is_error is False
    assert result.structured_content["data"]["allowed"] is False
    assert result.structured_content["data"]["recorded"]["status"] == "failed"
    assert len(requests) == 1


def test_unexpected_recording_exception_keeps_known_settlement(
    monkeypatch, caplog, capsys
):
    def unexpected(**kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(_record, "request_ingest", unexpected)
    requests, _, _ = _install_http(
        monkeypatch, responses=[httpx.Response(204)], credentials=[_key()]
    )
    result = anyio.run(_call_tool, "budget_confirm", CONFIRM)
    _assert_private(result, caplog, capsys)
    assert result.is_error is True
    assert result.structured_content["data"]["reservation_id"] == "res_known"
    assert result.structured_content["data"]["recorded"]["status"] == "unknown"
    assert len(requests) == 1


class _RawProtocolCall(BaseModel):
    name_param: ClassVar[None] = None
    method: str = "tools/call"
    params: dict


@pytest.mark.parametrize(
    "params",
    [
        {"name": "budget_check", "arguments": CANARY},
        {"name": {"private": CANARY}, "arguments": {}},
    ],
)
def test_malformed_tool_envelope_has_safe_protocol_error_and_diagnostics(
    monkeypatch, caplog, capsys, params
):
    calls = []
    monkeypatch.setattr(auth, "resolve_credential", lambda **_: calls.append("auth"))

    async def exercise():
        async with Client(mcp_server.create_server(), cache=None) as session:
            caplog.set_level(logging.DEBUG)
            logging.warning("An unrelated diagnostic remains available.")
            with pytest.raises(MCPError) as raised:
                await session.session.send_request(
                    _RawProtocolCall(params=params), types.CallToolResult
                )
            assert raised.value.code == types.INVALID_PARAMS
            assert CANARY not in str(raised.value)
            assert len((await session.list_tools()).tools) == 37

    anyio.run(exercise)
    assert calls == []
    assert CANARY not in caplog.text
    assert CANARY not in repr([(r.msg, r.args, r.exc_info) for r in caplog.records])
    # SDK 2 may reject these envelopes without emitting a diagnostic.
    assert "An unrelated diagnostic remains available." in caplog.text
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err


def test_malformed_notification_is_private_and_session_remains_usable(
    monkeypatch, caplog, capsys
):
    monkeypatch.setattr(auth, "resolve_credential", lambda **_: None)

    async def exercise():
        async with Client(mcp_server.create_server(), cache=None) as session:
            caplog.set_level(logging.DEBUG)
            await session.session.send_notification(
                _RawProtocolCall(
                    method="notifications/cancelled",
                    params={"requestId": {CANARY: CANARY}},
                )
            )
            assert len((await session.list_tools()).tools) == 37

    anyio.run(exercise)
    assert CANARY not in caplog.text
    assert CANARY not in repr([(r.msg, r.args, r.exc_info) for r in caplog.records])
    # SDK 2 may reject these envelopes without emitting a diagnostic.
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err


def test_unexpected_ancillary_failure_preserves_all_completed_reservations(
    monkeypatch, caplog, capsys
):
    decisions = [
        {**CHECK_RESPONSE, "reservation_id": f"known_{index}"} for index in range(32)
    ]
    decisions[1] = {**CHECK_RESPONSE, "allowed": False, "reservation_id": None}
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=journal(decisions))],
        credentials=[_key()],
    )

    def fail(**kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(budget, "denial_event", fail)
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK] * 32}
    )
    _assert_private(result, caplog, capsys)
    assert result.is_error
    data = result.structured_content["data"]
    assert [entry["result"]["reservation_id"] for entry in data["items"]] == [
        None if index == 1 else f"known_{index}" for index in range(32)
    ]
    assert data["operation_id"] == ID
    assert len(requests) == 1


def test_denial_receipt_exception_retains_entire_validated_batch(
    monkeypatch, caplog, capsys
):
    decisions = [
        {**CHECK_RESPONSE, "reservation_id": f"known_{index}"} for index in range(32)
    ]
    decisions[1] = {**CHECK_RESPONSE, "allowed": False, "reservation_id": None}
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=journal(decisions))],
        credentials=[_key()],
    )
    processed = []

    def receipt(**kwargs):
        processed.append(kwargs["event"].call_id)
        raise RuntimeError(CANARY)

    monkeypatch.setattr(budget, "record_best_effort", receipt)
    result = anyio.run(
        _call_tool, "budget_check_batch", {"operation_id": ID, "checks": [CHECK] * 32}
    )
    _assert_private(result, caplog, capsys)
    assert result.is_error
    data = result.structured_content["data"]
    assert [entry["result"]["reservation_id"] for entry in data["items"]] == [
        None if index == 1 else f"known_{index}" for index in range(32)
    ]
    retry = data["items"][1]["recording_retry"]
    assert processed == [retry["event"]["call_id"]]
    assert data["items"][1]["result"]["recorded"]["status"] == "unknown"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
def test_batch_does_not_convert_cancellation_or_process_control_to_failure(
    monkeypatch, failure
):
    requests = []
    _install_http(monkeypatch, responses=[], credentials=[_key()])

    def transport(self, request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json={"results": [{"result": CHECK_RESPONSE}] * 32}
            )
        raise failure()

    monkeypatch.setattr(httpx.MockTransport, "handle_request", transport)
    with mcp_server.tool_call_client(mcp_server.McpLaunchOptions()) as (_, client):
        with pytest.raises(failure):
            budget.request_budget_check_batch(
                client=client,
                checks=budget.prepare_batch_checks([CHECK] * 65),
                no_record=True,
            )
    assert len(requests) == 2
