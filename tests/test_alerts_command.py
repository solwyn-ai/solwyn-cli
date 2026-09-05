"""Alert history and owner configuration behavior."""

import copy
from contextlib import contextmanager
import json
import shlex
from pathlib import Path
import httpx
import pytest
from typer.testing import CliRunner
from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import error_from_response
from solwyn_cli.commands import alerts
from solwyn_cli.main import app
from test_budget_set_command import FakeClient, ROUTING

runner = CliRunner()
STAMP = "2026-09-01T00:00:00Z"
WINDOW = dict(
    requested_from=STAMP,
    requested_to="2026-09-05T00:00:00Z",
    effective_from=STAMP,
    effective_to="2026-09-05T00:00:00Z",
    retention_clamped=False,
)
LEG = dict(
    leg_key="slack:one",
    channel_type="slack",
    channel_name="Operations",
    status="failed",
    attempt_count=3,
    last_attempted_at=STAMP,
    delivered_at=None,
    error="Channel unavailable",
)
EVENT = dict(
    id="event-one",
    project_id="proj_alpha",
    account_id="account-one",
    scope="project",
    rule_key="rule-one",
    emitted_at=STAMP,
    event_type="triggered",
    rule_type="scoped_budget",
    value_at_event=12.5,
    threshold_at_event=10,
    period_key=None,
    linked_event_ids=[],
    metadata_json={"scope": "tag"},
    delivery=dict(
        status="partial",
        attempt_count=3,
        next_attempt_at=None,
        dead_lettered_at=None,
        legs=[LEG],
    ),
)
ACCOUNT = dict(
    account_id="account-one",
    spend_threshold_usd=100,
    enabled=True,
    channel_ids=["ch-one"],
    snoozed_until=None,
    routing=ROUTING,
    created_at=STAMP,
    updated_at=STAMP,
)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(
        FakeClient,
        "current_credential",
        property(lambda self: self.credential),
        raising=False,
    )
    monkeypatch.setattr(alerts, "SolwynClient", FakeClient)
    monkeypatch.setattr(
        alerts,
        "resolve_credential",
        lambda: auth.JwtCredential(access_token="access", refresh_token="refresh"),
    )


def invoke(args, fmt="json"):
    return runner.invoke(
        app, ["--format", fmt, "--project", "proj_alpha", "alerts", *args]
    )


def page(events=None, cursor=None, **window):
    return httpx.Response(
        200,
        json=dict(
            events=[copy.deepcopy(EVENT)] if events is None else events,
            next_cursor=cursor,
            window={**WINDOW, **window},
        ),
    )


@pytest.mark.parametrize("fmt", ["table", "json"])
@pytest.mark.parametrize("command", ["list", "show"])
def test_history_golden(fmt, command):
    FakeClient.responses = [page()]
    result = invoke([command, *(["event-one"] if command == "show" else [])], fmt)
    assert result.exit_code == 0, result.output
    assert_matches_golden(
        result.stdout,
        Path("tests/golden")
        / f"alerts-{command}-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )


def test_cursor_walk_filters_failed_locally_and_keeps_params():
    delivered = {
        **EVENT,
        "id": "other",
        "delivery": {**EVENT["delivery"], "status": "delivered"},
    }
    FakeClient.responses = [page([delivered], "next"), page()]
    result = invoke(
        [
            "list",
            "--all",
            "--failed-only",
            "--scope",
            "tag",
            "--rule-type",
            "scoped_budget",
        ]
    )
    assert result.exit_code == 0, result.output
    assert [e["id"] for e in json.loads(result.stdout)["events"]] == ["event-one"]
    requests = FakeClient.instances[0].requests
    assert requests[0][2]["params"] == {
        "range": "30d",
        "scope": "tag",
        "rule_type": "scoped_budget",
    }
    assert requests[1][2]["params"]["cursor"] == "next"


def test_show_walks_and_not_found():
    FakeClient.responses = [page([], "next"), page()]
    assert invoke(["show", "event-one", "--range", "7d"]).exit_code == 0
    assert len(FakeClient.instances[0].requests) == 2
    FakeClient.responses = [page([])]
    assert invoke(["show", "absent"]).exit_code == 5


def test_cycle_fails():
    FakeClient.responses = [page([], "same"), page([], "same")]
    assert invoke(["list", "--all"]).exit_code == 1


@pytest.mark.parametrize(
    "flags", [["--range", "365d"], ["--from", STAMP], ["--scope", "x" * 201]]
)
def test_bad_query_no_request(flags):
    assert invoke(["list", *flags]).exit_code == 2
    assert not FakeClient.instances


@pytest.mark.parametrize(
    "operation", ["show", "set", "delete", "snooze", "unsnooze", "preview"]
)
@pytest.mark.parametrize("fmt", ["json", "table"])
def test_account_golden(operation, fmt):
    payload = (
        ACCOUNT
        if operation in {"show", "set"}
        else ROUTING
        if operation == "preview"
        else {"snoozed_until": STAMP if operation == "snooze" else None}
    )
    FakeClient.responses = [
        httpx.Response(204)
        if operation == "delete"
        else httpx.Response(200, json=payload)
    ]
    flags = (
        ["--spend", "100", "--channel", "ch-one"]
        if operation == "set"
        else ["--until", STAMP]
        if operation == "snooze"
        else []
    )
    result = invoke(["account", operation, *flags], fmt)
    assert result.exit_code == 0, result.output
    assert_matches_golden(
        result.stdout,
        Path("tests/golden")
        / f"alerts-account-{operation}-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )
    method, path, kwargs = FakeClient.instances[0].requests[0]
    assert path == "/api/v1/account/alerts" + (
        "/" + operation if operation in {"snooze", "unsnooze", "preview"} else ""
    )
    if operation == "set":
        assert kwargs["json"] == {
            "spend_threshold_usd": 100.0,
            "channel_ids": ["ch-one"],
        }
    if operation == "unsnooze":
        assert "json" not in kwargs


@pytest.mark.parametrize(
    "args",
    [["list", "--account"], ["account", "show"], ["account", "set", "--spend", "10"]],
)
def test_account_requires_jwt(monkeypatch, args):
    monkeypatch.setattr(
        alerts,
        "resolve_credential",
        lambda: auth.ProjectKeyCredential(
            api_key="sk_proj_" + "ab" * 32, source="environment"
        ),
    )
    result = invoke(args)
    assert result.exit_code == 3, result.output
    assert not FakeClient.instances


@pytest.mark.parametrize(
    "status,detail,exit_code,text",
    [
        (403, "owner_required", 3, "whoami"),
        (403, "This endpoint requires JWT authentication", 3, "solwyn login"),
        (403, {"code": "read_only_key", "message": "Read-only key"}, 3, "full-scope"),
        (
            402,
            {
                "code": "upgrade_required",
                "feature": "account_spend_alert",
                "message": "Account spend alerts require Production",
                "allowed_values": ["7d"],
                "cta_path": "/settings/billing",
            },
            1,
            "Allowed values",
        ),
        (
            422,
            {
                "code": "unknown_notification_channel",
                "message": "Unknown channel",
                "channel_ids": ["ch-missing"],
            },
            2,
            "ch-missing",
        ),
        (
            404,
            {
                "code": "account_alert_not_found",
                "message": "No account alert configured.",
            },
            5,
            "account_alert_not_found",
        ),
    ],
)
def test_account_remote_errors(status, detail, exit_code, text):
    FakeClient.responses = [
        error_from_response(httpx.Response(status, json={"detail": detail}))
    ]
    result = invoke(["account", "set", "--spend", "10"])
    assert result.exit_code == exit_code, result.output
    assert text in result.output
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize(
    "enabled,expected", [([], None), (["--enabled"], True), (["--disabled"], False)]
)
def test_set_preserves_omitted_channels_and_enabled(enabled, expected):
    FakeClient.responses = [httpx.Response(200, json=ACCOUNT)]
    result = invoke(["account", "set", "--spend", "10", *enabled])
    assert result.exit_code == 0, result.output
    body = FakeClient.instances[0].requests[0][2]["json"]
    assert "channel_ids" not in body
    assert ("enabled" in body) == (expected is not None)
    if expected is not None:
        assert body["enabled"] is expected


@pytest.mark.parametrize(
    "flags",
    [
        ["set", "--spend", "nan"],
        ["set", "--spend", "0"],
        ["snooze", "--until", "2026-09-10T12:00:00"],
    ],
)
def test_bad_account_input_before_auth(flags):
    assert invoke(["account", *flags]).exit_code == 2
    assert not FakeClient.instances


def test_account_history_path_and_custom_window():
    FakeClient.responses = [
        page(
            [{**EVENT, "project_id": None, "scope": "account"}], retention_clamped=True
        )
    ]
    result = invoke(
        ["list", "--account", "--from", STAMP, "--to", "2026-09-05T00:00:00Z"]
    )
    assert result.exit_code == 0, result.output
    assert "Retention clamped" in result.stderr
    method, path, kwargs = FakeClient.instances[0].requests[0]
    assert path == "/api/v1/account/alert-events"
    assert kwargs["params"] == {"from": STAMP, "to": "2026-09-05T00:00:00Z"}


@pytest.mark.parametrize(
    "status",
    ["pending", "retrying", "processed", "delivered", "partial", "failed", None],
)
def test_failed_only_rollup(status):
    event = {
        **EVENT,
        "delivery": None if status is None else {**EVENT["delivery"], "status": status},
    }
    FakeClient.responses = [page([event], "next")]
    result = invoke(["list", "--failed-only"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert bool(body["events"]) == (status in {"partial", "failed"})
    assert body["next_cursor"] == "next"
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize("field", ["event", "leg", "delivery", "window"])
def test_response_parsers_remain_strict(field):
    event = copy.deepcopy(EVENT)
    window = copy.deepcopy(WINDOW)
    target = {
        "event": event,
        "leg": event["delivery"]["legs"][0],
        "delivery": event["delivery"],
        "window": window,
    }[field]
    target["unexpected"] = "new field"
    FakeClient.responses = [
        httpx.Response(
            200, json={"events": [event], "window": window, "next_cursor": None}
        )
    ]
    assert invoke(["list"]).exit_code == 1


@pytest.mark.parametrize("fmt", ["csv", "json"])
@pytest.mark.parametrize("account", [True, False])
def test_stream_export_raw_bytes_headers_and_no_filters(monkeypatch, fmt, account):
    body = (
        b"id,rule_key\n'=formula,rule\n"
        if fmt == "csv"
        else b'{"id":"one"}\n{"id":"two"}\n'
    )
    response = httpx.Response(
        200,
        content=body,
        headers={
            "X-Solwyn-Retention-Clamped": "true",
            "X-Solwyn-Effective-From": STAMP,
        },
    )

    @contextmanager
    def stream(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        try:
            yield response
        finally:
            response.close()

    monkeypatch.setattr(FakeClient, "stream", stream, raising=False)
    result = invoke(["export", "--format", fmt, *(["--account"] if account else [])])
    assert result.exit_code == 0, result.output
    assert result.stdout.encode() == body
    assert "X-Solwyn-Retention-Clamped: true" in result.stderr
    assert "exclude delivery" in result.stderr
    assert response.is_closed
    method, path, kwargs = FakeClient.instances[0].requests[0]
    assert path == (
        "/api/v1/account/alert-events/export"
        if account
        else "/api/v1/projects/proj_alpha/alert-events/export"
    )
    assert kwargs["params"] == {"range": "30d", "format": fmt}


@pytest.mark.parametrize("status", [401, 429, 503, None])
def test_mutations_only_retry_definite_rejections(monkeypatch, status):
    real_client = SolwynClient
    requests = []
    responses = [
        httpx.Response(
            status, json={"detail": "rejected"}, headers={"Retry-After": "0"}
        )
        if status
        else httpx.ConnectError("offline"),
        httpx.Response(200, json=ACCOUNT),
    ]

    def handle(request):
        if request.url.path.endswith("/auth/refresh"):
            return httpx.Response(
                200,
                json={
                    "access_token": "new",
                    "refresh_token": "refresh",
                    "token_type": "bearer",
                },
            )
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client_factory(**kwargs):
        return real_client(
            **kwargs, transport=httpx.MockTransport(handle), sleeper=lambda _: None
        )

    monkeypatch.setattr(alerts, "SolwynClient", client_factory)
    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access_token, refresh_token: auth.JwtCredential(
            access_token=access_token, refresh_token=refresh_token
        ),
    )
    result = invoke(["account", "set", "--spend", "10"])
    assert len(requests) == (2 if status in {401, 429} else 1)
    assert result.exit_code == (0 if status in {401, 429} else 1), result.output


def test_documented_filtered_json_invocation():
    command = next(
        line
        for line in Path("docs/commands/alerts.mdx").read_text().splitlines()
        if line.startswith("solwyn ") and "--failed-only --all" in line
    )
    FakeClient.responses = [page()]
    result = runner.invoke(app, ["--project", "proj_alpha", *shlex.split(command)[1:]])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["events"][0]["id"] == "event-one"
    assert FakeClient.instances[0].requests[0][2]["params"] == {
        "range": "30d",
        "rule_type": "scoped_budget",
        "scope": "tag",
    }
