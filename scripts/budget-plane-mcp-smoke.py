#!/usr/bin/env python3
"""Run the same staging scenarios through an actual stdio MCP server.

The CLI supplies an independent event-level ledger oracle: MCP exposes cost
totals and run rollups, but has no cost-events tool. Fixture administration and
the CLI-only utilization exit gate are shared with budget-plane-cli-smoke.py.
"""

import math
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import anyio
from mcp import ClientSession, MCPError
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from budget_plane_smoke import (
    EPSILON,
    INPUT,
    MODEL,
    OUTPUT,
    ROOT,
    Cli,
    Fixture,
    Smoke,
    SmokeFailure,
    main,
    recorded,
    replayed_recording,
    require,
)
from solwyn_cli._vendor.batch import new_operation_id


SCHEMAS = {
    "budget_status": "solwyn.mcp.budget-status.v1",
    "budget_check": "solwyn.mcp.budget-check.v1",
    "budget_confirm": "solwyn.mcp.budget-confirm.v1",
    "budget_check_batch": "solwyn.budget.check.batch.v1",
    "record_call": "solwyn.mcp.record.v1",
    "get_cost_total": "solwyn.mcp.cost-total.v1",
    "get_costs": "solwyn.mcp.costs.v1",
    "list_agent_runs": "solwyn.mcp.agent-runs.v1",
    "get_agent_run": "solwyn.mcp.agent-run.v1",
    "list_alert_events": "solwyn.alerts.v1",
}
WRITES = {"budget_check", "budget_confirm", "budget_check_batch", "record_call"}
DIAGNOSTIC_CODES = {
    "invalid_arguments",
    "invalid_budget_check_batch_request",
    "invalid_budget_check_batch_response",
    "invalid_budget_check_response",
    "invalid_budget_confirm_response",
    "batch_check_failed",
    "api_error",
    "not_found",
    "internal_error",
    "recording_unknown",
    "authentication_failed",
    "quota_exceeded",
    "operation_in_future",
    "operation_not_found",
    "invalid_operation_id",
    "operation_conflict",
    "rate_limited",
    "transport_error",
    "forbidden",
}


def unwrap(
    result: CallToolResult, name: str, *, replay_call_id: str | None = None
) -> dict[str, Any]:
    """A denial is ok=true/allowed=false; execution errors must never pass."""
    body = result.structured_content
    require(isinstance(body, dict), f"MCP {name}: missing structured result")
    require(body.get("schema") == SCHEMAS[name], f"MCP {name}: incorrect result schema")
    if replay_call_id is not None and result.is_error:
        require(
            name == "budget_confirm"
            and body.get("ok") is False
            and (body.get("error") or {}).get("code") == "recording_unknown",
            "MCP exact replay returned an unexpected error",
        )
        data = body.get("data")
        require(
            isinstance(data, dict) and data.get("settlement") == "not_submitted",
            "MCP exact replay lost its known settlement state",
        )
        require(
            data.get("recorded", {}).get("status") == "unknown",
            "MCP exact replay hid recording uncertainty",
        )
        replayed_recording(data, "MCP exact replay", replay_call_id)
        require(
            data["recorded"]["duplicates"][0]["reason"] == "legacy_key",
            "MCP exact replay error was not a legacy collision",
        )
        return data
    error = body.get("error") or {}
    code = error.get("code")
    diagnostic = (
        code if isinstance(code, str) and code in DIAGNOSTIC_CODES else "unclassified"
    )
    status = error.get("status_code")
    http_status = (
        f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
    )
    require(
        not result.is_error,
        f"MCP {name}: tool execution failed ({diagnostic}{http_status})",
    )
    require(
        body.get("ok") is True and body.get("error") is None,
        f"MCP {name}: unsuccessful result",
    )
    require(isinstance(body.get("data"), dict), f"MCP {name}: missing data")
    return body["data"]


@asynccontextmanager
async def open_session(
    cli: Cli, profile: str
) -> AsyncIterator[tuple[ClientSession, str, dict[str, Any]]]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from solwyn_cli.main import app; app()",
            "--api-url",
            cli.api_url,
            "--project",
            cli.project_id,
            "mcp",
            "--profile",
            profile,
        ],
        env=cli.env,
        cwd=ROOT,
    )
    # Server stderr is deliberately not retained: a regression could print keys.
    with open(os.devnull, "w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                with anyio.fail_after(cli.timeout):
                    await session.discover()
                    require(
                        session.server_info is not None
                        and session.server_info.name == "solwyn",
                        "MCP initialized a different server",
                    )
                    tools = await session.list_tools()
                yield (
                    session,
                    session.protocol_version,
                    {tool.name: tool for tool in tools.tools},
                )


async def verify_read_profile(cli: Cli) -> None:
    async with open_session(cli, "read") as (session, _, tools):
        require(not WRITES & tools.keys(), "MCP read profile exposed writes")
        require(
            "budget_status" in tools
            and all(
                tool.annotations and tool.annotations.read_only_hint
                for tool in tools.values()
            ),
            "MCP read inventory lost read annotations",
        )
        with anyio.fail_after(cli.timeout):
            try:
                await session.call_tool("budget_check", {})
            except MCPError as error:
                require(
                    error.code == -32602,
                    "MCP read profile returned the wrong protocol error",
                )
            else:
                raise SmokeFailure("MCP read profile accepted a hidden write tool")


class McpSmoke(Smoke):
    def __init__(
        self,
        cli: Cli,
        fixture: Fixture,
        wait_seconds: float,
        session: ClientSession,
        skip_batch: bool = False,
    ):
        super().__init__(cli, fixture, wait_seconds, skip_batch)
        self.session = session
        self.tool_calls = 0

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with anyio.fail_after(self.cli.timeout):
            result = await self.session.call_tool(name, arguments)
        replay = arguments.get("record_only") if name == "budget_confirm" else None
        return unwrap(
            result, name, replay_call_id=replay["call_id"] if replay else None
        )

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        self.tool_calls += 1
        # The shared synchronous scenarios run in an AnyIO worker. Calls cross
        # back to the one live protocol session, never into server internals.
        try:
            return anyio.from_thread.run(self._call, name, arguments)
        except SmokeFailure:
            raise
        except Exception:
            raise SmokeFailure(
                f"MCP {name}: protocol failure or timeout; not replayed"
            ) from None

    def check(self, run: str, *, denied: bool = False) -> dict[str, Any]:
        data = self.call(
            "budget_check",
            model=MODEL,
            provider="openai",
            estimated_input_tokens=INPUT,
            estimated_output_tokens=OUTPUT,
            agent_run_id=run,
        )
        require(data["project_id"] == self.cli.project_id, "MCP check: wrong project")
        require(
            data["allowed"] is not denied, "MCP check: incorrect admission decision"
        )
        if denied:
            require(
                data.get("reservation_id") is None, "MCP denial created a reservation"
            )
            recorded(data, "MCP denial")
            self.track(run, denied=True)
        else:
            require(
                bool(data.get("reservation_id")),
                "MCP check failed open without a reservation",
            )
        return data

    def confirm(self, reservation: str, run: str, *, replay: bool = False) -> None:
        arguments = {
            "reservation_id": reservation,
            "call_id": str(uuid4()),
            "model": MODEL,
            "provider": "openai",
            "input_tokens": INPUT,
            "output_tokens": OUTPUT,
            "agent_run_id": run,
            "tags": {"smoke": "cli"},
        }
        data = self.call("budget_confirm", **arguments)
        require(data["settlement"] == "confirmed", "MCP settlement was not confirmed")
        recorded(data, "MCP confirm")
        self.track(run)
        if replay:
            repeated = self.call("budget_confirm", **arguments, no_record=True)
            require(
                repeated["settlement"] == "confirmed"
                and repeated["recorded"]["status"] == "skipped",
                "MCP settlement replay did not skip ingest",
            )
            retried = self.call("budget_confirm", record_only=data["recording_retry"])
            require(
                retried["settlement"] == "not_submitted",
                "MCP record-only submitted settlement",
            )
            if retried["recorded"]["status"] != "unknown":
                replayed_recording(retried, "MCP record-only", data["call_id"])

    def batch(self, batch_run: str) -> None:
        operation_id = new_operation_id()
        checks = [
            {
                "model": MODEL,
                "provider": "openai",
                "estimated_input_tokens": INPUT,
                "agent_run_id": batch_run,
            }
            for _ in range(2)
        ]
        submitted = self.call(
            "budget_check_batch", operation_id=operation_id, checks=checks
        )
        require(
            submitted["mode"] == "submit" and submitted["status"] == "finished",
            "MCP batch did not finish submission",
        )
        recovered = self.call(
            "budget_check_batch", operation_id=operation_id, recover_only=True
        )
        require(
            recovered["operation_id"] == operation_id
            and recovered["mode"] == "recover",
            "MCP batch recovery lost operation identity",
        )
        require(
            recovered["items"] == submitted["items"]
            and recovered["expires_at"] == submitted["expires_at"],
            "MCP recovery changed original outcomes or deadlines",
        )
        require(not recovered["recordings"], "MCP recovery unexpectedly ingested")
        require(
            len(submitted["items"]) == 2
            and all(item["state"] == "completed" for item in submitted["items"]),
            "MCP batch lost indexed outcomes",
        )
        reservations = [
            item["result"]["reservation_id"]
            for item in submitted["items"]
            if item["result"]["allowed"]
        ]
        require(
            len(reservations) == 2
            and all(reservations)
            and len(set(reservations)) == 2,
            "MCP batch reservations missing or reused",
        )
        # Use the original submit decisions; recovery does not renew authority.
        for reservation in reservations:
            self.confirm(reservation, batch_run)
        self.passed_phase(
            "MCP durable batch, GET recovery, original deadlines, settlement"
        )

    def record_calls(self, record_run: str) -> None:
        for _ in range(2):
            arguments = {
                "model": MODEL,
                "provider": "openai",
                "call_id": str(uuid4()),
                "input_tokens": INPUT,
                "output_tokens": OUTPUT,
                "agent_run_id": record_run,
                "tags": {"smoke": "cli"},
            }
            recorded(self.call("record_call", **arguments), "MCP record")
            self.track(record_run)
            recorded(
                self.call("record_call", **arguments),
                "MCP record replay",
                status="deduplicated",
            )

    def read_alerts(self) -> dict[str, Any]:
        return self.call("list_alert_events", time_range="7d", all_pages=True)

    def ledger(self) -> float | bool:
        cost = super().ledger()
        if cost is False:
            return False
        total = self.call("get_cost_total", time_range="7d")
        status = self.call("budget_status")
        success = sum(counts["success"] for counts in self.runs.values())
        require(
            total["project_id"] == self.cli.project_id
            and status["project_id"] == self.cli.project_id,
            "MCP read returned another project",
        )
        require(
            total["call_count"] == success
            and total["total_input_tokens"] == success * INPUT
            and total["total_output_tokens"] == success * OUTPUT,
            "MCP totals lost calls or tokens",
        )
        require(
            math.isclose(total["total_cost"], cost, abs_tol=EPSILON),
            "MCP and CLI costs disagree",
        )
        require(
            math.isclose(status["current_usage"], cost, abs_tol=EPSILON),
            "MCP budget counter differs from the ledger",
        )
        groups = self.call("get_costs", time_range="7d", group_by="model")
        require(
            not groups["truncated"]
            and math.isclose(
                sum(group["total_cost"] for group in groups["groups"]),
                cost,
                abs_tol=EPSILON,
            ),
            "MCP grouped cost report differs from ledger",
        )
        runs = self.call("list_agent_runs", time_range="7d", limit=100)
        require(not runs["has_more"], "Unexpected MCP run page")
        for name, counts in self.runs.items():
            found = [run for run in runs["runs"] if run["name"] == name]
            require(
                len(found) == 1 and found[0]["total_calls"] == sum(counts.values()),
                "MCP run list lost attribution",
            )
            detail = self.call("get_agent_run", run=found[0]["id"])
            require(detail == found[0], "MCP run detail differs from list")
        return cost


async def exercise_async(
    cli: Cli, fixture: Fixture, wait_seconds: float, skip_batch: bool
) -> dict[str, Any]:
    await verify_read_profile(cli)
    async with open_session(cli, "guardrails") as (session, protocol, tools):
        require(
            SCHEMAS.keys() <= tools.keys(),
            "MCP guardrails inventory is missing required tools",
        )
        require(
            all(
                tools[name].annotations
                and tools[name].annotations.read_only_hint is False
                for name in WRITES
            ),
            "MCP writes were advertised as reads",
        )
        smoke = McpSmoke(cli, fixture, wait_seconds, session, skip_batch)
        # Keep the session's event loop alive while the scenarios use synchronous
        # CLI subprocesses and bounded polling for the independent ledger oracle.
        summary = await anyio.to_thread.run_sync(smoke.run)
        summary.update(
            mcp_tool_calls=smoke.tool_calls,
            mcp_protocol=protocol,
            mcp_tools=len(tools),
            read_profile="passed",
        )
        return summary


def exercise(
    cli: Cli, fixture: Fixture, wait_seconds: float, skip_batch: bool = False
) -> dict[str, Any]:
    return anyio.run(exercise_async, cli, fixture, wait_seconds, skip_batch)


if __name__ == "__main__":
    raise SystemExit(main(transport="mcp", exercise=exercise))
