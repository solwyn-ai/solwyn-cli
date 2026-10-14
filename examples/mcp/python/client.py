"""A read-first Solwyn client. See docs/mcp-quickstart.md; no provider calls."""

import argparse
from contextlib import redirect_stderr
import json
import os
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

import anyio
from mcp import Client
from mcp.types import ListToolsResult
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from pydantic import BaseModel, ConfigDict, Field


SAFE_CODES = frozenset(
    {
        "authentication_required",
        "authentication_failed",
        "invalid_arguments",
        "forbidden",
        "read_only_key",
        "jwt_required",
    }
)
PROJECT_PATTERN = r"^proj_[a-f0-9]{24}$"
READ_TOOL_NAMES = frozenset(
    {
        "budget_status",
        "get_project_status",
        "get_forecast",
        "get_costs",
        "list_projects",
        "get_cost_total",
        "list_budget_rules",
        "list_tags",
        "list_tag_values",
        "list_agent_runs",
        "get_agent_run",
        "list_alert_events",
        "get_overview",
        "list_cost_events",
        "get_cost_baseline",
        "get_unit_economics",
        "get_cost_drift",
        "list_priced_models",
        "simulate_costs",
        "list_provider_failovers",
        "list_untracked_surfaces",
        "list_audit_events",
        "plan_status",
        "plan_catalog",
        "get_whoami",
        "get_account_phone",
        "list_alert_activity",
        "list_open_alert_deliveries",
        "budget_uncounted",
        "get_account_alert",
        "plan_preview",
    }
)


def validate_read_catalog(catalog: ListToolsResult) -> None:
    """Require the complete current read profile, including budget_status."""
    if (
        len(catalog.tools) != len(READ_TOOL_NAMES)
        or {tool.name for tool in catalog.tools} != READ_TOOL_NAMES
        or catalog.next_cursor is not None
        or not all(
            tool.annotations and tool.annotations.read_only_hint is True
            for tool in catalog.tools
        )
    ):
        raise ValueError("Unexpected read catalog")


class ToolError(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    code: str


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    schema_name: str = Field(alias="schema")
    ok: bool
    data: dict | None
    error: ToolError | None


class BudgetRead(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    project_id: str = Field(pattern=PROJECT_PATTERN)
    remaining_budget: float


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    allowed: bool
    reservation_id: str | None = Field(min_length=1)
    run_control: dict | None = None
    denied_by_period: str | None = None
    reason: Literal["run_terminated"] | None = None


def decode_result(is_error: bool, payload: object) -> Envelope:
    """Fail closed on a malformed envelope or contradictory protocol flags."""
    result = Envelope.model_validate(payload)
    if is_error == result.ok or result.ok != (
        result.data is not None and result.error is None
    ):
        raise ValueError("Inconsistent tool result")
    if not result.ok and result.error is None:
        raise ValueError("Missing execution error")
    return result


def provider_gate(result: Envelope) -> Literal["denied", "unreserved", "reserved"]:
    """Illustrate decision handling only; callers must still honor expiry/run state.

    Reads never authorize spend. Never use this helper to replay a check or to
    wrap provider calls that the Solwyn SDK already instruments.
    """
    if not result.ok or result.schema_name != "solwyn.mcp.budget-check.v1":
        raise ValueError("No successful budget decision")
    decision = Decision.model_validate(result.data)
    if (not decision.allowed and decision.reservation_id is not None) or (
        decision.allowed
        and (
            decision.run_control is not None
            or decision.denied_by_period is not None
            or decision.reason is not None
        )
    ):
        raise ValueError("Contradictory budget decision")
    if not decision.allowed:
        return "denied"
    return "unreserved" if decision.reservation_id is None else "reserved"


def safe_error_code(result: Envelope) -> str:
    return (
        result.error.code
        if result.error and result.error.code in SAFE_CODES
        else "execution_error"
    )


def server_environment() -> dict[str, str]:
    # The public transport also adds this documented OS baseline. Do not copy
    # os.environ: provider keys, tracing and Python bootstrap settings stay out.
    environment = get_default_environment()
    if "SOLWYN_API_KEY" in os.environ:
        # Preserve even empty presence so an invalid selection cannot fall back
        # to a different keychain identity. Values never enter argv or output.
        environment["SOLWYN_API_KEY"] = os.environ["SOLWYN_API_KEY"]
    return environment


async def read_budget(args: argparse.Namespace) -> tuple[dict, int]:
    params = StdioServerParameters(
        command=args.executable,
        args=[
            "--project",
            args.project,
            "--api-url",
            args.api_url,
            "mcp",
            "--profile",
            "read",
        ],
        env=server_environment(),
    )
    # The timeout cancels the request and unwinds the SDK context, which owns
    # stdin close, termination and bounded reaping. No abandoned work thread.
    with open(os.devnull, "w") as errlog, anyio.fail_after(args.timeout):
        async with Client(
            stdio_client(params, errlog=errlog),
            mode="legacy" if args.mode == "legacy" else "auto",
            cache=None,
            read_timeout_seconds=args.timeout,
        ) as client:
            expected = "2025-11-25" if args.mode == "legacy" else "2026-07-28"
            if client.protocol_version != expected:
                raise ValueError("Unexpected protocol")
            catalog = await client.list_tools()
            validate_read_catalog(catalog)
            arguments = (
                {"projectid": "PRIVATE_VALIDATION_CANARY"}
                if args.validation_error
                else {}
            )
            wire = await client.call_tool("budget_status", arguments)
            result = decode_result(wire.is_error, wire.structured_content)
            if not result.ok:
                return {
                    "status": "execution_error",
                    "code": safe_error_code(result),
                    "protocol": expected,
                }, 1
            if result.schema_name != "solwyn.mcp.budget-status.v1":
                raise ValueError("Unexpected read schema")
            data = BudgetRead.model_validate(result.data)
            if data.project_id != args.project:
                raise ValueError("Project mismatch")
            return {
                "status": "ok",
                "protocol": expected,
                "tools": len(catalog.tools),
                **data.model_dump(),
            }, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--executable", required=True, help="Absolute installed Solwyn executable"
    )
    parser.add_argument("--project", required=True, help="Selected read project")
    parser.add_argument("--api-url", default="https://api.solwyn.ai")
    parser.add_argument("--mode", choices=["modern", "legacy"], default="modern")
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="Useful-work seconds, 0.1–30; SDK cleanup is additional",
    )
    parser.add_argument(
        "--validation-error",
        action="store_true",
        help="Demonstrate a safe rejected argument without HTTP",
    )
    args = parser.parse_args()
    try:
        url = urlsplit(args.api_url)
        if (
            not Path(args.executable).is_absolute()
            or not Path(args.executable).is_file()
            or not re.fullmatch(PROJECT_PATTERN, args.project)
        ):
            raise ValueError("Invalid launch selection")
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Invalid API URL")
        if not 0.1 <= args.timeout <= 30:
            raise ValueError("Invalid deadline")
        # Only this standalone client process suppresses arbitrary SDK logging.
        # External host instrumentation is a different boundary from Solwyn.
        with open(os.devnull, "w") as private_stderr, redirect_stderr(private_stderr):
            output, code = anyio.run(read_budget, args)
    except (Exception, KeyboardInterrupt):
        output, code = (
            {"status": "client_error", "code": "connection_or_read_failed"},
            1,
        )
    print(json.dumps(output))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
