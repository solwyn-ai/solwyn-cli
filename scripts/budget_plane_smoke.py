#!/usr/bin/env python3
"""Exercise a deployed budget plane through real Solwyn CLI subprocesses.

Run: SOLWYN_JWT=... make smoke-cli
See docs/staging-smoke.md for coverage, credentials, and durable side effects.
Only disposable-project setup/configuration/archival uses HTTP with the JWT;
all reservations, settlements, metadata, and assertions use the CLI entrypoint.
No provider requests are made. Synthetic usage DOES consume staging quota and
create durable spend and real budget alerts. Credentials stay in memory.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
STAGING = "https://api-staging.solwyn.ai"
MODEL = "gpt-4.1"
INPUT = 1000
OUTPUT = 500
EPSILON = 0.000001


class SmokeFailure(RuntimeError):
    """A safe diagnostic that contains no captured output or credentials."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def info(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def failure_message(error: BaseException) -> str:
    if isinstance(error, SmokeFailure):
        return str(error)
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(failure_message(nested) for nested in error.exceptions)
    return f"unexpected {type(error).__name__}; response bodies suppressed"


def api_origin(value: str) -> str:
    origin = value.rstrip("/").removesuffix("/api/v1")
    parsed = urlsplit(origin)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    require(
        origin == STAGING
        or (
            local
            and parsed.scheme in {"http", "https"}
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ),
        "Smoke tests require the staging origin or a loopback API URL.",
    )
    return origin


def poll(label: str, probe: Callable[[], Any], *, wait_seconds: float) -> Any:
    """Retry only a not-yet-converged read, never a write or invalid response."""
    deadline = time.monotonic() + wait_seconds
    while True:
        result = probe()
        if result:
            return result
        remaining = deadline - time.monotonic()
        require(remaining > 0, f"{label}: did not converge within {wait_seconds:g}s")
        time.sleep(min(2, remaining))


def recorded(data: dict[str, Any], label: str, *, status: str = "ingested") -> None:
    receipt = data.get("recorded", data)
    observed = receipt.get("status")
    safe_status = (
        observed
        if observed
        in {
            "ingested",
            "deduplicated",
            "rejected",
            "skipped",
            "failed",
            "stopped",
            "unknown",
        }
        else "invalid"
    )
    require(
        observed == status, f"{label}: recording was {safe_status}, expected {status}"
    )
    require(not receipt.get("rejected"), f"{label}: recording rejected an event")
    if status == "ingested":
        require(
            receipt.get("ingested", 0) > 0, f"{label}: recording ingested no events"
        )


def replayed_recording(data: dict[str, Any], label: str, call_id: str) -> None:
    receipt = data["recorded"]
    require(
        receipt["ingested"] == 0 and not receipt["rejected"],
        f"{label}: replay wrote or rejected an event",
    )
    duplicates = receipt["duplicates"]
    require(
        len(duplicates) == 1
        and duplicates[0]["call_id"] == call_id
        and duplicates[0]["index"] == 0,
        f"{label}: duplicate identity differs",
    )
    if duplicates[0]["reason"] == "legacy_key":
        # Core probes committed timestamp/instance uniqueness before call_id.
        # Exact-event replay can therefore hit legacy_key. The CLI correctly
        # stays conservative; the later independent ledger checks prove no drift.
        require(
            receipt["status"] in {"failed", "unknown"},
            f"{label}: legacy collision falsely claimed durable call-ID acknowledgement",
        )
        info("Exact receipt replay hit legacy_key; verifying the ledger independently")
    else:
        require(
            duplicates[0]["reason"] == "call_id"
            and receipt["status"] == "deduplicated",
            f"{label}: invalid replay disposition",
        )


def alerts_ready(events: list[dict[str, Any]], project_id: str, run: str) -> bool:
    fired = [
        event
        for event in events
        if event["project_id"] == project_id and event["event_type"] == "fired"
    ]
    scoped = [event for event in fired if event["rule_type"] == "scoped_budget"]
    project = [event for event in fired if event["rule_type"] == "budget_threshold"]
    if not scoped or not project:
        return False
    require(
        len(scoped) == 1 and len(project) == 1, "Budget alerts fired more than once"
    )
    require(
        scoped[0]["metadata_json"].get("scope") == "agent_run"
        and scoped[0]["metadata_json"].get("match") == run,
        "Scoped alert named the wrong run",
    )
    for event in [scoped[0], project[0]]:
        require(
            event["value_at_event"] >= 0.001
            and math.isclose(event["threshold_at_event"], 0.001, abs_tol=EPSILON),
            "Alert spend or threshold differs from the installed cap",
        )
    return True


class Cli:
    def __init__(self, api_url: str, project_id: str, api_key: str, timeout: float):
        self.api_url = api_url
        self.project_id = project_id
        self.timeout = timeout
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SOLWYN_")}
        self.env["SOLWYN_API_KEY"] = api_key
        # A stored Solwyn login would otherwise win for user-plane reads.
        self.env["SOLWYN_AUTH"] = "project_key"
        self.env["PYTHONPATH"] = str(ROOT / "src")
        self.commands = 0

    def run(
        self,
        label: str,
        *args: str,
        schema: str,
        expected_exit: int = 0,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        # Same callable as the installed `solwyn` console script, on this uv
        # interpreter; no in-process CliRunner, mocked transports, or SDK calls.
        command = [
            sys.executable,
            "-c",
            "from solwyn_cli.main import app; app()",
            "--api-url",
            self.api_url,
            "--project",
            self.project_id,
            "--json",
            "--no-color",
            *args,
        ]
        self.commands += 1
        try:
            result = subprocess.run(
                command,
                input=stdin if stdin is not None else "",
                capture_output=True,
                text=True,
                env=self.env,
                cwd=ROOT,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SmokeFailure(
                f"{label}: CLI could not finish within its timeout"
            ) from None
        # Do not echo stdout/stderr: a regression could put secrets in either.
        require(
            result.returncode == expected_exit,
            f"{label}: CLI exit {result.returncode}, expected {expected_exit}",
        )
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise SmokeFailure(f"{label}: CLI did not return one JSON object") from None
        require(isinstance(data, dict), f"{label}: CLI JSON was not an object")
        require(data.get("schema") == schema, f"{label}: unexpected CLI schema")
        return data

    def status(self, **kwargs: Any) -> dict[str, Any]:
        return self.run(
            "budget status",
            "budget",
            "status",
            schema="solwyn.budget-status.v1",
            **kwargs,
        )


class Fixture:
    """Own only the project created by this invocation; never edit an existing one."""

    def __init__(self, client: httpx.Client, name: str):
        self.client = client
        self.name = name
        self.project_id: str | None = None

    def request(
        self, method: str, path: str, *, expected: int = 200, **kwargs: Any
    ) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise SmokeFailure(
                f"Fixture {method}: transport failure; not replayed"
            ) from None
        require(
            response.status_code == expected,
            f"Fixture {method}: HTTP {response.status_code}, expected {expected}",
        )
        if expected == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise SmokeFailure(f"Fixture {method}: invalid JSON") from None

    def create(self) -> str:
        data = self.request(
            "POST",
            "/api/v1/projects",
            expected=201,
            json={
                "name": self.name,
                "budget_limit": 1,
                "budget_period": "monthly",
                "budget_mode": "alert_only",
                "scope": "full",
            },
        )
        require(isinstance(data, dict), "Fixture creation: invalid project")
        project_id = data.get("id")
        require(
            isinstance(project_id, str)
            and re.fullmatch(r"proj_[0-9a-f]{24}", project_id) is not None,
            "Fixture creation: missing project ID",
        )
        self.project_id = project_id
        key = data.get("key")
        require(
            isinstance(key, str) and key.startswith("solwyn_sk_"),
            "Fixture creation: missing API key",
        )
        info(f"Created disposable project {self.project_id} ({self.name})")
        return key

    def budget(
        self,
        *,
        limit: float = 1,
        mode: str = "alert_only",
        scoped_run: str | None = None,
    ) -> None:
        require(self.project_id is not None, "No owned fixture project")
        rules: list[dict[str, Any]] = [{"type": "budget_threshold", "percent": 100}]
        if scoped_run:
            rules.append(
                {
                    "type": "scoped_budget",
                    "scope": "agent_run",
                    "match": scoped_run,
                    "limit": 0.001,
                    "mode": "hard_deny",
                }
            )
        self.request(
            "PUT",
            f"/api/v1/projects/{quote(self.project_id, safe='')}/budget",
            json={
                "limit_usd": limit,
                "period": "monthly",
                "mode": mode,
                "thresholds": rules,
            },
        )

    def archive(self) -> None:
        if self.project_id is not None:
            self.request(
                "DELETE",
                f"/api/v1/projects/{quote(self.project_id, safe='')}",
                expected=204,
            )
            info(f"Archived project {self.project_id}; its API keys are revoked")


class Smoke:
    def __init__(
        self, cli: Cli, fixture: Fixture, wait_seconds: float, skip_batch: bool = False
    ):
        self.cli = cli
        self.fixture = fixture
        self.wait_seconds = wait_seconds
        self.runs: dict[str, dict[str, int]] = {}
        self.passed: list[str] = []
        self.tags_visible = False
        self.skip_batch = skip_batch

    def passed_phase(self, name: str) -> None:
        self.passed.append(name)
        info(f"PASS {name}")

    def track(self, run: str, *, denied: bool = False) -> None:
        counts = self.runs.setdefault(run, {"success": 0, "denied": 0})
        counts["denied" if denied else "success"] += 1

    def check(self, run: str, *, denied: bool = False) -> dict[str, Any]:
        data = self.cli.run(
            "denied check" if denied else "allowed check",
            "budget",
            "check",
            "--model",
            MODEL,
            "--provider",
            "openai",
            "--estimated-input-tokens",
            str(INPUT),
            "--estimated-output-tokens",
            str(OUTPUT),
            "--agent-run-id",
            run,
            schema="solwyn.budget-check.v1",
            expected_exit=4 if denied else 0,
        )
        require(data["project_id"] == self.cli.project_id, "check: wrong project")
        require(data["allowed"] is not denied, "check: incorrect admission decision")
        if denied:
            require(data.get("reservation_id") is None, "denial created a reservation")
            recorded(data, "denial")
            self.track(run, denied=True)
        else:
            require(
                bool(data.get("reservation_id")),
                "allowed check failed open without a reservation",
            )
        return data

    def confirm(self, reservation: str, run: str, *, replay: bool = False) -> None:
        args = [
            "budget",
            "confirm",
            "--reservation-id",
            reservation,
            "--call-id",
            str(uuid4()),
            "--model",
            MODEL,
            "--provider",
            "openai",
            "--input-tokens",
            str(INPUT),
            "--output-tokens",
            str(OUTPUT),
            "--agent-run-id",
            run,
            "--tag",
            "smoke=cli",
        ]
        data = self.cli.run("confirm", *args, schema="solwyn.budget-confirm.v1")
        require(data["settlement"] == "confirmed", "confirm: settlement not confirmed")
        recorded(data, "confirm")
        self.track(run)
        if replay:
            repeated = self.cli.run(
                "settlement replay",
                *args,
                "--no-record",
                schema="solwyn.budget-confirm.v1",
            )
            require(
                repeated["settlement"] == "confirmed",
                "settlement replay was not acknowledged",
            )
            require(
                repeated["recorded"]["status"] == "skipped",
                "settlement replay unexpectedly ingested",
            )
            # Preserve the FIRST receipt, including timestamp and instance ID.
            with tempfile.TemporaryDirectory(prefix="solwyn-smoke-") as directory:
                event = Path(directory) / "receipt.json"
                event.write_text(json.dumps(data["recording_retry"]))
                retried = self.cli.run(
                    "recording replay",
                    "budget",
                    "confirm",
                    "--record-only",
                    str(event),
                    schema="solwyn.budget-confirm.v1",
                )
            require(
                retried["settlement"] == "not_submitted",
                "record-only submitted settlement",
            )
            replayed_recording(retried, "record-only", data["call_id"])

    def ledger(self) -> float | bool:
        success = sum(counts["success"] for counts in self.runs.values())
        denied = sum(counts["denied"] for counts in self.runs.values())
        total = self.cli.run(
            "cost totals",
            "costs",
            "total",
            "--range",
            "7d",
            schema="solwyn.costs.total.v1",
        )
        events = self.cli.run(
            "cost events",
            "costs",
            "events",
            "--range",
            "7d",
            "--limit",
            "100",
            schema="solwyn.costs.events.v1",
        )
        require(
            not events["has_more"], "Unexpected extra ledger page in disposable project"
        )
        items = events["items"]
        if total["call_count"] != success or len(items) != success + denied:
            return False
        require(
            total["project_id"] == self.cli.project_id, "cost totals: wrong project"
        )
        require(
            total["total_input_tokens"] == success * INPUT,
            "Cost input tokens differ (possible duplicate receipt)",
        )
        require(
            total["total_output_tokens"] == success * OUTPUT,
            "Cost output tokens differ",
        )
        require(
            total["unpriced_call_count"] == 0 and total["local_call_count"] == 0,
            "Synthetic calls were not priced as OpenAI",
        )
        paid = [item for item in items if item["status"] == "success"]
        refused = [item for item in items if item["status"] == "budget_denied"]
        require(
            len(paid) == success and len(refused) == denied, "Incorrect ledger statuses"
        )
        for event in paid:
            require(
                event["input_tokens"] == INPUT and event["output_tokens"] == OUTPUT,
                "Ledger event token mismatch",
            )
            require(
                event["cost"] > 0,
                "Paid event lost cost",
            )
            require(
                event["tags"] == ({"smoke": "cli"} if self.tags_visible else None),
                "Event tags differ from the plan's visibility contract",
            )
        require(
            all(
                event["cost"] == 0 and event["output_tokens"] == 0 for event in refused
            ),
            "Denied receipts were charged",
        )
        cost = total["total_cost"]
        require(
            cost > 0
            and math.isclose(
                cost, sum(event["cost"] for event in paid), abs_tol=EPSILON
            ),
            "Ledger total differs from event costs",
        )
        status = self.cli.status()
        if not math.isclose(status["current_usage"], cost, abs_tol=EPSILON):
            return False
        run_list = self.cli.run(
            "run rollups",
            "runs",
            "list",
            "--range",
            "7d",
            "--limit",
            "100",
            schema="solwyn.runs.v1",
        )
        require(not run_list["has_more"], "Unexpected extra run page")
        for name, counts in self.runs.items():
            matches = [run for run in run_list["runs"] if run["name"] == name]
            if len(matches) != 1 or matches[0]["total_calls"] != sum(counts.values()):
                return False
            run = matches[0]
            run_events = [
                event for event in items if event["agent_run_id"] == run["id"]
            ]
            require(
                len(run_events) == run["total_calls"],
                "Run attribution lost ledger events",
            )
            require(
                math.isclose(
                    run["total_cost"],
                    sum(event["cost"] for event in run_events),
                    abs_tol=EPSILON,
                ),
                "Run cost differs from ledger",
            )
            require(
                run["total_input_tokens"]
                == sum(event["input_tokens"] for event in run_events),
                "Run input tokens differ from ledger",
            )
            require(
                run["total_output_tokens"]
                == sum(event["output_tokens"] for event in run_events),
                "Run output tokens differ from ledger",
            )
        return cost

    def batch(self, batch_run: str) -> None:
        body = [
            {
                "model": MODEL,
                "provider": "openai",
                "estimated_input_tokens": INPUT,
                "agent_run_id": batch_run,
            }
            for _ in range(2)
        ]
        batch = self.cli.run(
            "batch check",
            "budget",
            "check",
            "--batch",
            "-",
            stdin=json.dumps(body),
            schema="solwyn.budget.check.batch.v1",
        )
        require(len(batch["results"]) == 2, "Batch lost a result")
        reservations = [
            item["result"]["reservation_id"]
            for item in batch["results"]
            if item.get("result") and item["result"]["allowed"]
        ]
        require(
            len(reservations) == 2
            and all(reservations)
            and len(set(reservations)) == 2,
            "Batch reservations were missing or reused",
        )
        for reservation in reservations:
            self.confirm(reservation, batch_run)
        self.passed_phase("batch reservation and settlement")

    def record_calls(self, record_run: str) -> None:
        body = [
            {
                "model": MODEL,
                "provider": "openai",
                "call_id": str(uuid4()),
                "input_tokens": INPUT,
                "output_tokens": OUTPUT,
                "agent_run_id": record_run,
                "tags": {"smoke": "cli"},
            }
            for _ in range(2)
        ]
        receipt = self.cli.run(
            "record batch",
            "record",
            "--batch",
            "-",
            stdin=json.dumps(body),
            schema="solwyn.record.v1",
        )
        recorded(receipt, "record batch")
        require(receipt["ingested"] == 2, "Record batch lost an event")
        for _ in body:
            self.track(record_run)
        receipt = self.cli.run(
            "record batch replay",
            "record",
            "--batch",
            "-",
            stdin=json.dumps(body),
            schema="solwyn.record.v1",
        )
        recorded(receipt, "record batch replay", status="deduplicated")

    def read_alerts(self) -> dict[str, Any]:
        return self.cli.run(
            "alerts",
            "alerts",
            "list",
            "--range",
            "7d",
            "--all",
            schema="solwyn.alerts.v1",
        )

    def run(self) -> dict[str, Any]:
        prefix = self.fixture.name
        plan = self.cli.run("plan capabilities", "plan", schema="solwyn.plan.v1")
        self.tags_visible = plan["capabilities"]["analytics"]["tags"]
        require(
            plan["capabilities"]["analytics"]["cost_events_paginated"],
            "Full ledger smoke requires Production or higher cost-event access",
        )
        require(
            "hard_deny" in plan["capabilities"]["budget"]["allowed_modes"],
            "Smoke requires hard-deny entitlement",
        )
        info(
            f"Plan {plan['tier']}: checking {'visible' if self.tags_visible else 'redacted'} event tags"
        )
        self.fixture.budget()
        require(
            self.cli.status()["current_usage"] == 0, "New project already has spend"
        )
        single = f"{prefix}-single"
        self.confirm(self.check(single)["reservation_id"], single, replay=True)
        self.passed_phase("reservation, confirm, settlement replay, recording replay")
        if self.skip_batch:
            info("SKIP batch admission/recovery (explicit --skip-batch)")
        else:
            self.batch(f"{prefix}-batch")
        self.record_calls(f"{prefix}-record")
        poll(
            "initial ledger/counter/run reconciliation",
            self.ledger,
            wait_seconds=self.wait_seconds,
        )
        self.passed_phase(
            "standalone ingest, deduplication, durable floor, run rollups"
        )

        self.fixture.budget(scoped_run=single)
        self.check(single, denied=True)
        fresh = f"{prefix}-fresh"
        self.confirm(self.check(fresh)["reservation_id"], fresh)
        self.passed_phase("scoped hard denial with an unaffected fresh run")

        self.fixture.budget(limit=0.001, mode="hard_deny")
        self.check(f"{prefix}-denied", denied=True)
        self.cli.run(
            "utilization gate",
            "budget",
            "status",
            "--max-utilization",
            "100",
            schema="solwyn.budget-status.v1",
            expected_exit=4,
        )
        self.fixture.budget(limit=0.001, mode="alert_only")
        over = f"{prefix}-over"
        self.confirm(self.check(over)["reservation_id"], over)
        self.passed_phase("project hard denial, status exit gate, alert-only overspend")

        def probe_alerts() -> bool:
            data = self.read_alerts()
            return alerts_ready(data["events"], self.cli.project_id, single)

        poll(
            "scoped and project alert events",
            probe_alerts,
            wait_seconds=self.wait_seconds,
        )
        self.passed_phase("scoped and project budget alert history")
        cost = poll(
            "final ledger/counter/run reconciliation",
            self.ledger,
            wait_seconds=self.wait_seconds,
        )
        self.passed_phase(
            "final ledger, counter, denied receipts, tags, and run reconciliation"
        )
        return {
            "phases": self.passed,
            "successful_calls": sum(c["success"] for c in self.runs.values()),
            "denied_calls": sum(c["denied"] for c in self.runs.values()),
            "durable_cost_usd": cost,
            "cli_commands": self.cli.commands,
            "tier": plan["tier"],
            "tags_visible": self.tags_visible,
            "skipped_phases": ["batch"] if self.skip_batch else [],
        }


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def main(
    *,
    transport: str = "cli",
    exercise: Callable[[Cli, Fixture, float, bool], dict[str, Any]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("SOLWYN_API_URL", STAGING))
    parser.add_argument(
        "--wait-seconds",
        type=positive_seconds,
        default=60.0,
        help="Read convergence deadline per phase (default 60)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=30.0,
        help="Timeout per CLI subprocess / fixture HTTP request (default 30)",
    )
    parser.add_argument(
        "--keep-project",
        action="store_true",
        help="Keep the created project and its durable spend for dashboard inspection",
    )
    parser.add_argument(
        "--skip-batch",
        action="store_true",
        help="Explicitly omit batch coverage; reported as skipped in the summary",
    )
    args = parser.parse_args()
    fixture = None
    summary: dict[str, Any] = {
        "schema": f"solwyn.smoke.{transport}.v1",
        "passed": False,
    }
    try:
        origin = api_origin(args.api_url)
        jwt = os.environ.get("SOLWYN_JWT", "").strip()
        require(bool(jwt), "SOLWYN_JWT is required; obtain an account JWT for staging.")
        name = f"smoke-{transport}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid4().hex[:6]}"
        summary.update(api_url=origin, project_name=name)
        info(
            "Synthetic usage creates durable staging spend, consumes quota, and can send budget alerts."
        )
        with httpx.Client(
            base_url=origin,
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=args.timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            fixture = Fixture(client, name)
            try:
                key = fixture.create()
                summary["project_id"] = fixture.project_id
                cli = Cli(origin, fixture.project_id, key, args.timeout)
                summary.update(
                    exercise(cli, fixture, args.wait_seconds, args.skip_batch)
                    if exercise
                    else Smoke(cli, fixture, args.wait_seconds, args.skip_batch).run()
                )
                summary["passed"] = True
            finally:
                if fixture.project_id:
                    summary["project_id"] = fixture.project_id
                    summary["archived"] = False
                    if args.keep_project:
                        info(
                            f"Kept project {fixture.project_id} for dashboard inspection"
                        )
                    else:
                        fixture.archive()
                        summary["archived"] = True
    except (SmokeFailure, KeyboardInterrupt) as error:
        summary["passed"] = False
        info(f"FAIL {error if isinstance(error, SmokeFailure) else 'interrupted'}")
    except Exception as error:
        summary["passed"] = False
        # No raw exception: validation and subprocess errors can contain secrets.
        info(f"FAIL {failure_message(error)}")
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["passed"] else 1
