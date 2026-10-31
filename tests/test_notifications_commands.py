"""Notification settings, reusable channels and alert-email behavior."""

import importlib
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app
from login_fixtures import login_credential

runner = CliRunner()
BASE = "/api/v1/projects/proj%2Fone/notifications"
CHANNELS = "/api/v1/account/notification-channels"
EMAIL = "/api/v1/account/alert-email"
SECRET = "synthetic-secret-abcd"
CHANNEL = {
    "id": "ch_one",
    "account_id": "acct_one",
    "channel_type": "slack",
    "name": "Ops",
    "slack_config": {"webhook_url": "****abcd", "bot_token": None, "channel": None},
    "created_at": "2026-09-01T12:00:00Z",
    "updated_at": "2026-09-02T12:00:00Z",
}
CONFIG = {
    "channel_ids": ["ch_one"],
    "channels": [CHANNEL],
    "sms_config": {"enabled": False},
    "email": {"enabled": True},
    "severity_threshold": "warning",
    "enabled_event_types": [],
    "capabilities": {
        "available_channels": ["email", "sms", "slack"],
        "sms_allowed_events": ["budget_exceeded"],
        "email_allowed_events": ["budget_threshold"],
    },
}
ALERT_EMAIL = {"effective_email": "ops@example.com", "override": "ops@example.com"}


def test_notification_surfaces_registered():
    result = runner.invoke(app, ["notifications", "--help"])
    assert result.exit_code == 0
    for command in ("show", "set", "test"):
        assert command in result.stdout


@pytest.fixture
def api(monkeypatch):
    module = importlib.import_module("solwyn_cli.commands._notifications_common")
    calls = []
    responses = []
    monkeypatch.setenv(
        "SOLWYN_CONFIG_DIR", "/private/tmp/solwyn-notification-test-config"
    )
    monkeypatch.delenv("SOLWYN_CHANNEL_SECRET", raising=False)
    monkeypatch.setattr(
        module,
        "resolve_credential",
        lambda **_: login_credential("synthetic-jwt"),
    )

    def request(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(module.SolwynClient, "request", request)
    return module, calls, responses


def invoke(args, *, input=None, json_output=True):
    return runner.invoke(
        app,
        ["--project", "proj/one", "--json" if json_output else "--no-color", *args],
        input=input,
    )


@pytest.mark.parametrize("format", ["table", "json"])
@pytest.mark.parametrize(
    "args,body,golden",
    [
        (["notifications", "show"], CONFIG, "notifications-show"),
        (["notifications", "set", "--no-email"], CONFIG, "notifications-set"),
        (
            ["notifications", "test", "sms"],
            {
                "success": True,
                "message": "SMS eligibility confirmed (no message sent).",
            },
            "notifications-test",
        ),
        (["channels", "list"], [CHANNEL], "channels-list"),
        (["channels", "show", "ch_one"], CHANNEL, "channels-show"),
        (
            [
                "channels",
                "create",
                "--type",
                "slack",
                "--name",
                "Ops",
                "--secret-stdin",
            ],
            CHANNEL,
            "channels-create",
        ),
        (["channels", "update", "ch_one", "--name", "Ops"], CHANNEL, "channels-update"),
        (
            ["channels", "test", "ch_one"],
            {"success": True, "message": "Test sent"},
            "channels-test",
        ),
        (["channels", "delete", "--yes", "ch_one"], None, "channels-delete"),
        (["account", "alert-email", "show"], ALERT_EMAIL, "account-alert-email-show"),
        (
            ["account", "alert-email", "set", "ops@example.com"],
            ALERT_EMAIL,
            "account-alert-email-set",
        ),
        (
            ["account", "alert-email", "clear"],
            {"effective_email": "owner@example.com", "override": None},
            "account-alert-email-clear",
        ),
    ],
)
def test_command_goldens(api, format, args, body, golden):
    _, _, responses = api
    responses.append(
        httpx.Response(
            204 if body is None else 201 if "create" in args else 200,
            **({} if body is None else {"json": body}),
        )
    )
    result = invoke(args, input=SECRET + "\n", json_output=format == "json")
    assert result.exit_code == 0, result.output
    assert_matches_golden(
        result.stdout,
        Path("tests/golden")
        / f"{golden}-{format}.{'json' if format == 'json' else 'txt'}",
    )
    if "delete" in args and args[0] == "channels":
        assert (
            result.stderr
            == "Delete channel 'ch_one' in the authenticated account: removes the channel and its project notification and budget-rule routing references.\n"
        )
    else:
        assert result.stderr == ""


@pytest.mark.parametrize(
    "flags,expected",
    [
        (["--channel", "one", "--channel", "two"], {"channel_ids": ["one", "two"]}),
        (["--clear-channels"], {"channel_ids": []}),
        (["--all-events"], {"enabled_event_types": []}),
        (
            ["--event", "provider_outage", "--event", "budget_exceeded"],
            {"enabled_event_types": ["budget_exceeded", "provider_outage"]},
        ),
        (["--severity", "critical"], {"severity_threshold": "critical"}),
        (
            ["--no-email", "--sms"],
            {"email_config": {"enabled": False}, "sms_config": {"enabled": True}},
        ),
        (["--reset-email", "--reset-sms"], {"email_config": None, "sms_config": None}),
    ],
)
def test_config_partial_update(api, flags, expected):
    _, calls, responses = api
    responses.append(httpx.Response(200, json=CONFIG))
    result = invoke(["notifications", "set", *flags])
    assert result.exit_code == 0, result.output
    assert calls[0][:2] == ("PUT", BASE)
    assert calls[0][2]["json"] == expected


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--channel", "one", "--clear-channels"],
        ["--event", "budget_exceeded", "--all-events"],
        # Core's NotificationEventType enum rejects unknown names; so does the CLI.
        ["--event", "future_event"],
        ["--email", "--reset-email"],
        ["--sms", "--reset-sms"],
        ["--severity", "invalid"],
    ],
)
def test_invalid_settings_no_http(api, flags):
    _, calls, _ = api
    result = invoke(["notifications", "set", *flags])
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize(
    "type,secret,extra,config",
    [
        ("slack", SECRET, [], {"webhook_url": SECRET}),
        (
            "slack",
            SECRET,
            ["--secret-field", "bot_token", "--channel", "C123"],
            {"bot_token": SECRET, "channel": "C123"},
        ),
        ("pagerduty", SECRET, [], {"routing_key": SECRET}),
        ("teams", SECRET, [], {"webhook_url": SECRET}),
        ("discord", SECRET, [], {"webhook_url": SECRET}),
        (
            "telegram",
            SECRET,
            ["--chat-id", "-123"],
            {"bot_token": SECRET, "chat_id": "-123"},
        ),
    ],
)
def test_create_channel_types(api, monkeypatch, type, secret, extra, config):
    _, calls, responses = api
    monkeypatch.setenv("SOLWYN_CHANNEL_SECRET", secret)
    responses.append(httpx.Response(201, json=CHANNEL))
    result = invoke(["channels", "create", "--type", type, "--name", "Ops", *extra])
    assert result.exit_code == 0, result.output
    assert calls[0][:2] == ("POST", CHANNELS)
    assert calls[0][2]["json"] == {
        "channel_type": type,
        "name": "Ops",
        f"{type}_config": config,
    }
    assert secret not in result.output


@pytest.mark.parametrize(
    "args,input",
    [
        (["--type", "slack", "--secret-stdin"], "****abcd"),
        (["--type", "telegram", "--secret-stdin"], SECRET),
        (["--type", "slack", "--secret-field", "bot_token", "--secret-stdin"], SECRET),
        (
            ["--type", "teams", "--secret-field", "routing_key", "--secret-stdin"],
            SECRET,
        ),
        (["--type", "teams", "--chat-id", "1", "--secret-stdin"], SECRET),
        (["--type", "slack"], None),
    ],
)
def test_invalid_channel_configuration_no_http(api, args, input):
    _, calls, _ = api
    result = invoke(["channels", "create", "--name", "Ops", *args], input=input)
    assert result.exit_code == 2, result.output
    assert SECRET not in result.output
    assert calls == []


def test_update_masked_secret(api):
    _, calls, responses = api
    responses.extend([httpx.Response(200, json=CHANNEL)] * 2)
    result = invoke(
        ["channels", "update", "id/one", "--secret-stdin"], input="****abcd\n"
    )
    assert result.exit_code == 0, result.output
    assert calls[0][:2] == ("GET", CHANNELS + "/id%2Fone")
    assert calls[1][2]["json"] == {"slack_config": {"webhook_url": "****abcd"}}


@pytest.mark.parametrize("channel", ["slack", "pagerduty", "sms", "email"])
def test_notification_test_shapes_and_failure(api, monkeypatch, channel):
    _, calls, responses = api
    monkeypatch.setenv("SOLWYN_CHANNEL_SECRET", SECRET)
    responses.append(
        httpx.Response(
            200,
            json={"delivered": False}
            if channel == "email"
            else {"success": False, "message": "Delivery failed"},
        )
    )
    result = invoke(["notifications", "test", channel])
    assert result.exit_code == 1, result.output
    assert calls[0][:2] == ("POST", BASE + "/test-" + channel)
    if channel in ("slack", "pagerduty"):
        assert calls[0][2]["json"] == {
            "webhook_url" if channel == "slack" else "routing_key": SECRET
        }
    assert "failed" in result.output.lower() or "false" in result.output.lower()
    assert SECRET not in result.output


def test_channel_test_false_exits_one(api):
    _, _, responses = api
    responses.append(
        httpx.Response(200, json={"success": False, "message": "Delivery failed"})
    )
    result = invoke(["channels", "test", "ch_one"])
    assert result.exit_code == 1
    assert "Delivery failed" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["channels", "list"],
        ["channels", "show", "id"],
        ["channels", "delete", "--yes", "id"],
        ["account", "alert-email", "show"],
        ["account", "alert-email", "set", "ops@example.com"],
        ["notifications", "test", "email"],
    ],
)
def test_jwt_only_routes_reject_project_keys(api, monkeypatch, args):
    module, calls, _ = api
    monkeypatch.setattr(
        module,
        "resolve_credential",
        lambda **_: auth.ProjectKeyCredential(
            api_key="sk_proj_" + "ab" * 32, source="environment"
        ),
    )
    result = invoke(args)
    assert result.exit_code == 3
    assert "login" in result.output
    assert calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["notifications", "show"],
        ["notifications", "set", "--sms"],
        ["notifications", "test", "sms"],
    ],
)
def test_project_routes_allow_keys(api, monkeypatch, args):
    module, _, responses = api
    monkeypatch.setattr(
        module,
        "resolve_credential",
        lambda **_: auth.ProjectKeyCredential(
            api_key="sk_proj_" + "ab" * 32, source="environment"
        ),
    )
    responses.append(
        httpx.Response(
            200, json={"success": True, "message": "sent"} if "test" in args else CONFIG
        )
    )
    assert invoke(args).exit_code == 0


def test_secret_echo_in_api_error_redacted(api, monkeypatch, caplog):
    _, _, responses = api
    monkeypatch.setenv("SOLWYN_CHANNEL_SECRET", SECRET)
    responses.append(
        error_from_response(
            httpx.Response(
                422,
                json={"detail": {"code": "bad_config", "message": "Invalid " + SECRET}},
            )
        )
    )
    result = invoke(["channels", "create", "--type", "slack", "--name", "Ops"])
    assert result.exit_code == 1
    assert SECRET not in result.output + caplog.text


def test_unmasked_server_secret_is_defensively_masked(api):
    _, _, responses = api
    responses.append(
        httpx.Response(200, json={**CHANNEL, "slack_config": {"webhook_url": SECRET}})
    )
    result = invoke(["channels", "show", "ch_one"])
    assert result.exit_code == 0
    assert SECRET not in result.output
    assert "****abcd" in result.output


@pytest.mark.parametrize(
    "code,status",
    [
        ("owner_required", 403),
        ("notification_channel_not_found", 404),
        ("upgrade_required", 402),
    ],
)
def test_channel_api_error_contract(api, code, status):
    _, _, responses = api
    responses.append(
        error_from_response(
            httpx.Response(
                status,
                json={
                    "detail": {
                        "code": code,
                        "message": "SMS requires Production",
                        "required_tier": "production",
                        "feature": "notifications.sms",
                        "allowed_values": ["email"],
                        "cta_path": "/billing",
                    }
                },
            )
        )
    )
    result = invoke(["channels", "test", "ch_one"])
    assert result.exit_code == {403: 3, 404: 5, 402: 1}[status]
    if status == 402:
        assert (
            "Production" in result.output
            and "/billing" in result.output
            and "email" in result.output
        )
    if status == 403:
        assert "whoami" in result.output


def test_alert_email_body_and_clear(api):
    _, calls, responses = api
    responses.extend([httpx.Response(200, json=ALERT_EMAIL)] * 2)
    assert invoke(["account", "alert-email", "set", "ops@example.com"]).exit_code == 0
    assert calls[0][:2] == ("PUT", EMAIL)
    assert calls[0][2]["json"] == {"override": "ops@example.com"}
    assert invoke(["account", "alert-email", "clear"]).exit_code == 0
    assert calls[1][:2] == ("DELETE", EMAIL)


def test_invalid_email_no_http(api):
    _, calls, _ = api
    assert invoke(["account", "alert-email", "set", "not-an-email"]).exit_code == 2
    assert calls == []


def test_masked_write_back_stays_masked_in_output(api):
    _, _, responses = api
    responses.extend([httpx.Response(200, json=CHANNEL)] * 2)
    result = invoke(
        ["channels", "update", "ch_one", "--secret-stdin"], input="****abcd\n"
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["slack_config"]["webhook_url"] == "****abcd"


@pytest.mark.parametrize(
    "kind,config",
    [
        ("pagerduty", {"routing_key": SECRET, "api_token": "synthetic-api-token-efgh"}),
        (
            "slack",
            {
                "webhook_url": SECRET,
                "bot_token": "synthetic-bot-token-efgh",
                "channel": "C123",
            },
        ),
        ("telegram", {"bot_token": SECRET, "chat_id": "-123"}),
    ],
)
def test_config_stdin_supports_all_fields(api, kind, config):
    _, calls, responses = api
    responses.append(httpx.Response(201, json=CHANNEL))
    result = invoke(
        ["channels", "create", "--type", kind, "--name", "Ops", "--config-stdin"],
        input=json.dumps(config),
    )
    assert result.exit_code == 0, result.output
    assert calls[0][2]["json"][f"{kind}_config"] == config
    assert SECRET not in result.output


def test_config_stdin_null_clears_only_selected_field(api):
    _, calls, responses = api
    responses.extend([httpx.Response(200, json=CHANNEL)] * 2)
    result = invoke(
        ["channels", "update", "ch_one", "--config-stdin"], input='{"bot_token": null}'
    )
    assert result.exit_code == 0
    assert calls[1][2]["json"] == {"slack_config": {"bot_token": None}}


@pytest.mark.parametrize(
    "input",
    [
        "invalid secret " + SECRET,
        '{"webhook_url": 3}',
        '{"slack_config": {"webhook_url": "' + SECRET + '"}}',
        "[]",
    ],
)
def test_bad_json_configuration_does_not_leak(api, input):
    _, calls, _ = api
    result = invoke(
        ["channels", "create", "--type", "slack", "--name", "Ops", "--config-stdin"],
        input=input,
    )
    assert result.exit_code == 2
    assert SECRET not in result.output
    assert calls == []


def test_delivery_response_secret_echo_redacted(api, monkeypatch):
    _, _, responses = api
    monkeypatch.setenv("SOLWYN_CHANNEL_SECRET", SECRET)
    responses.append(
        httpx.Response(200, json={"success": False, "message": "Failed for " + SECRET})
    )
    result = invoke(["notifications", "test", "slack"])
    assert result.exit_code == 1
    assert SECRET not in result.output
    assert "[REDACTED]" in result.output


@pytest.mark.parametrize(
    "args,payload",
    [
        (["notifications", "show"], {**CONFIG, "future_field": "extra"}),
        (
            ["channels", "show", "ch_one"],
            {**CHANNEL, "slack_config": {"future_secret": SECRET}},
        ),
        (["channels", "list"], {"channels": [CHANNEL]}),
        (["account", "alert-email", "show"], {**ALERT_EMAIL, "future_field": "extra"}),
    ],
)
def test_invalid_responses_fail_closed(api, args, payload):
    _, _, responses = api
    responses.append(httpx.Response(200, json=payload))
    result = invoke(args)
    assert result.exit_code == 1
    assert "invalid_notifications_response" in result.output
    assert SECRET not in result.output


def test_email_delivery_success_preserves_actual_wire_shape(api):
    _, _, responses = api
    responses.append(httpx.Response(200, json={"delivered": True}))
    result = invoke(["notifications", "test", "email"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["delivered"] is True


def test_delete_requires_204_without_replaying_unexpected_success(api):
    _, calls, responses = api
    responses.append(httpx.Response(200, json={}))
    result = invoke(["channels", "delete", "--yes", "ch_one"])
    assert result.exit_code == 1
    assert len(calls) == 1
    assert calls[0][2] == {}
