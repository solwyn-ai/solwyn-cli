"""MCP page budgets count filtered pages and retain the rejected-page cursor."""

from functools import partial

import anyio
import httpx
import pytest

from solwyn_cli import _mcp_operation as operation
from solwyn_cli.commands import alerts, runs
from solwyn_cli.errors import ApiError
from test_alerts_command import WINDOW
from test_runs_command import RUN, _list_response


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)


def run(work):
    async def exercise():
        async with operation.operation_scope() as control:
            try:
                return await operation.run_sync(work), control.recovery()
            except operation.OperationLimit as error:
                return error, control.recovery()

    return anyio.run(exercise)


def test_ten_empty_filtered_pages_stop_before_eleventh():
    client = Client(
        [
            {"events": [], "next_cursor": f"page-{i + 1}", "window": WINDOW}
            for i in range(11)
        ]
    )
    error, recovery = run(
        partial(
            alerts.request_alert_events,
            client=client,
            project_id="proj_alpha",
            all_pages=True,
            failed_only=True,
        )
    )
    assert isinstance(error, operation.OperationLimit)
    assert error.code == "page_limit"
    assert len(client.calls) == 10
    assert recovery.cursor == "page-10"


def test_named_run_repeated_cursor_never_selects_apparent_unique_match():
    client = Client([_list_response(runs=[RUN], next_cursor="same", has_more=True)] * 3)
    with pytest.raises(ValueError, match="cursor"):
        run(
            partial(
                runs.resolve_agent_run_id,
                client=client,
                project_id="proj_alpha",
                run=RUN["name"],
            )
        )
    assert len(client.calls) == 2


def test_response_limit_keeps_cursor_used_for_rejected_page():
    client = Client(
        [
            {"events": [], "next_cursor": "retry-this", "window": WINDOW},
            operation.OperationLimit("response_size_limit"),
        ]
    )
    error, recovery = run(
        partial(
            alerts.request_alert_events,
            client=client,
            project_id="proj_alpha",
            all_pages=True,
        )
    )
    assert error.code == "response_size_limit"
    assert recovery.cursor == "retry-this"


def test_entitlement_fallback_shares_the_same_ten_page_budget():
    denied = ApiError(
        status_code=402,
        code="upgrade_required",
        message="fixed",
        hint=None,
        detail={"allowed_values": ["7d"]},
    )
    client = Client(
        [denied]
        + [
            _list_response(runs=[RUN], next_cursor=f"next-{i}", has_more=True)
            for i in range(10)
        ]
    )
    error, recovery = run(
        partial(
            runs.resolve_agent_run_id,
            client=client,
            project_id="proj_alpha",
            run=RUN["name"],
        )
    )
    assert error.code == "page_limit"
    assert len(client.calls) == 10
    assert client.calls[0]["params"]["range"] == "365d"
    assert all(call["params"]["range"] == "7d" for call in client.calls[1:])
    assert recovery.cursor == "next-8"
