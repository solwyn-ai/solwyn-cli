"""Offline regression tests for the staging smoke runner's failure boundaries."""

import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/budget-plane-cli-smoke.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location(
        "budget_cli_smoke", SCRIPT.with_name("budget_plane_smoke.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("reason", "status"),
    [("call_id", "deduplicated"), ("legacy_key", "failed"), ("legacy_key", "unknown")],
)
def test_exact_replay_checks_duplicate_identity_and_conservative_status(
    smoke, reason, status
):
    data = {
        "recorded": {
            "status": status,
            "ingested": 0,
            "rejected": [],
            "duplicates": [{"index": 0, "reason": reason, "call_id": "original"}],
        }
    }
    smoke.replayed_recording(data, "replay", "original")
    data["recorded"]["duplicates"][0]["call_id"] = "different"
    with pytest.raises(smoke.SmokeFailure):
        smoke.replayed_recording(data, "replay", "original")


def test_exact_replay_rejects_double_ingest_and_false_success(smoke):
    data = {
        "recorded": {
            "status": "ingested",
            "ingested": 1,
            "duplicates": [],
            "rejected": [],
        }
    }
    with pytest.raises(smoke.SmokeFailure):
        smoke.replayed_recording(data, "replay", "original")
    data["recorded"].update(
        status="deduplicated",
        ingested=0,
        duplicates=[{"index": 0, "reason": "legacy_key", "call_id": "original"}],
    )
    with pytest.raises(smoke.SmokeFailure):
        smoke.replayed_recording(data, "replay", "original")


def test_missing_jwt_fails_without_printing_other_credentials(monkeypatch):
    monkeypatch.delenv("SOLWYN_JWT", raising=False)
    monkeypatch.setenv("SOLWYN_API_KEY", "private-project-key")
    result = subprocess.run(
        ["uv", "run", "--no-sync", "python", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "SOLWYN_JWT is required" in result.stderr
    assert "private-project-key" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "https://api.solwyn.ai",
        "https://api-staging.solwyn.ai.evil.example",
        "https://user:password@api-staging.solwyn.ai",
        "https://api-staging.solwyn.ai?token=private",
        "https://api-staging.solwyn.ai/unexpected",
        "http://api-staging.solwyn.ai",
    ],
)
def test_rejects_origins_that_could_send_credentials_or_spend_elsewhere(smoke, url):
    with pytest.raises(smoke.SmokeFailure):
        smoke.api_origin(url)


def test_accepts_staging_suffix_and_local_api(smoke):
    assert smoke.api_origin("https://api-staging.solwyn.ai/api/v1/") == (
        "https://api-staging.solwyn.ai"
    )
    assert smoke.api_origin("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    ("code", "body"),
    [(0, '{"schema":"wrong.v1"}'), (1, "private response"), (0, "not JSON")],
)
def test_cli_rejects_failed_or_malformed_success_without_raw_output(
    smoke, monkeypatch, code, body
):
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], code, body, "private stderr"
        ),
    )
    cli = smoke.Cli("https://api-staging.solwyn.ai", "project", "private-key", 10)
    with pytest.raises(smoke.SmokeFailure) as failure:
        cli.run("probe", "budget", "status", schema="solwyn.budget-status.v1")
    assert "private" not in str(failure.value)


def test_cli_checks_expected_denial_and_isolates_ambient_solwyn_env(smoke, monkeypatch):
    monkeypatch.setenv("SOLWYN_JWT", "private-jwt")
    monkeypatch.setenv("SOLWYN_AGENT_RUN_ID", "unrelated-run")
    monkeypatch.setenv("SOLWYN_TAGS", "unrelated=tag")
    monkeypatch.setenv("SOLWYN_API_KEY", "unrelated-key")
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            4,
            json.dumps({"schema": "solwyn.budget-check.v1", "allowed": False}),
            "",
        )

    monkeypatch.setattr(smoke.subprocess, "run", run)
    cli = smoke.Cli("https://api-staging.solwyn.ai", "project", "fixture-key", 10)
    result = cli.run(
        "denial", "budget", "check", schema="solwyn.budget-check.v1", expected_exit=4
    )
    assert result["allowed"] is False
    assert seen["env"]["SOLWYN_API_KEY"] == "fixture-key"
    assert not {"SOLWYN_JWT", "SOLWYN_AGENT_RUN_ID", "SOLWYN_TAGS"} & seen["env"].keys()
    assert seen["timeout"] == 10


def test_poll_reports_nonconvergence_and_never_retries_a_failure(smoke):
    with pytest.raises(smoke.SmokeFailure, match="ledger"):
        smoke.poll("ledger", lambda: False, wait_seconds=0)
    attempts = []

    def failed_read():
        attempts.append(1)
        raise smoke.SmokeFailure("broken response")

    with pytest.raises(smoke.SmokeFailure, match="broken response"):
        smoke.poll("ledger", failed_read, wait_seconds=10)
    assert len(attempts) == 1


def test_recording_warning_cannot_pass_as_durable_success(smoke):
    with pytest.raises(smoke.SmokeFailure, match="recording"):
        smoke.recorded({"recorded": {"status": "failed", "ingested": 0}}, "confirm")
    smoke.recorded({"recorded": {"status": "ingested", "ingested": 1}}, "confirm")


def test_async_exception_groups_preserve_safe_diagnostics_but_not_raw_errors(smoke):
    error = ExceptionGroup(
        "private group text",
        [smoke.SmokeFailure("MCP check: failed"), ValueError("private response")],
    )
    message = smoke.failure_message(error)
    assert "MCP check: failed" in message
    assert "ValueError" in message
    assert "private" not in message


@pytest.mark.parametrize(
    ("keep", "bad_key", "archive_status"),
    [(False, False, 204), (True, False, 204), (False, True, 204), (False, False, 500)],
)
def test_failure_cleanup_only_archives_the_created_project(
    smoke, monkeypatch, capsys, keep, bad_key, archive_status
):
    project_id = "proj_0123456789abcdef01234567"
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v1/projects":
            return httpx.Response(
                201,
                json={
                    "id": project_id,
                    "key": "invalid" if bad_key else "sk_proj_private-key",
                },
            )
        if (
            request.method == "DELETE"
            and request.url.path == f"/api/v1/projects/{project_id}"
        ):
            return httpx.Response(archive_status)
        raise AssertionError("Unexpected fixture request")

    real_client = httpx.Client
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("SOLWYN_JWT", "private-jwt")
    monkeypatch.setenv("SOLWYN_API_URL", "https://api-staging.solwyn.ai")
    monkeypatch.setattr(
        smoke.sys, "argv", ["smoke"] + (["--keep-project"] if keep else [])
    )

    def fail(*args):
        raise smoke.SmokeFailure("synthetic check failure")

    assert smoke.main(exercise=fail) == 1
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert summary["passed"] is False
    assert summary["project_id"] == project_id
    assert "private-jwt" not in output.out + output.err
    assert "sk_proj_private-key" not in output.out + output.err
    assert requests == [("POST", "/api/v1/projects")] + (
        [] if keep else [("DELETE", f"/api/v1/projects/{project_id}")]
    )
    if archive_status == 204:
        assert summary["archived"] is not keep
    else:
        assert summary["archived"] is False


@pytest.mark.parametrize("skip", [False, True])
def test_batch_coverage_can_only_be_disabled_explicitly(
    smoke, monkeypatch, capsys, skip
):
    project_id = "proj_0123456789abcdef01234567"
    real_client = httpx.Client

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                201, json={"id": project_id, "key": "sk_proj_private-key"}
            )
        return httpx.Response(204)

    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("SOLWYN_JWT", "private-jwt")
    monkeypatch.setenv("SOLWYN_API_URL", "https://api-staging.solwyn.ai")
    monkeypatch.setattr(
        smoke.sys, "argv", ["smoke"] + (["--skip-batch"] if skip else [])
    )

    def exercise(cli, fixture, wait_seconds, skip_batch):
        assert skip_batch is skip
        return {"skipped_phases": ["batch"] if skip_batch else []}

    assert smoke.main(exercise=exercise) == 0
    assert json.loads(capsys.readouterr().out)["skipped_phases"] == (
        ["batch"] if skip else []
    )


def test_alert_probe_cannot_pass_with_an_unrelated_scoped_event(smoke):
    events = [
        {
            "project_id": "project",
            "event_type": "fired",
            "rule_type": "scoped_budget",
            "value_at_event": 0.006,
            "threshold_at_event": 0.001,
            "metadata_json": {"scope": "agent_run", "match": "wrong-run"},
        },
        {
            "project_id": "project",
            "event_type": "fired",
            "rule_type": "budget_threshold",
            "value_at_event": 0.03,
            "threshold_at_event": 0.001,
            "metadata_json": {},
        },
    ]
    with pytest.raises(smoke.SmokeFailure):
        smoke.alerts_ready(events, "project", "capped-run")
    events[0]["metadata_json"]["match"] = "capped-run"
    assert smoke.alerts_ready(events, "project", "capped-run")
    assert not smoke.alerts_ready([], "project", "capped-run")
