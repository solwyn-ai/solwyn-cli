"""Real-client regressions for notification review findings (no live HTTP)."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import _notifications_common as common
from solwyn_cli.main import app

from test_notifications_commands import ALERT_EMAIL, CHANNEL, CONFIG

runner = CliRunner()
SMS_RESULT = {
    "success": True,
    "message": "SMS eligibility confirmed (no message sent).",
}


@pytest.fixture
def transport_api(monkeypatch: pytest.MonkeyPatch) -> tuple:
    responses: list[httpx.Response] = []
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    saved_tokens: list[tuple[str, str]] = []
    monkeypatch.delenv("SOLWYN_CHANNEL_SECRET", raising=False)
    monkeypatch.setattr(
        common,
        "resolve_credential",
        lambda: auth.JwtCredential(
            access_token="synthetic-access-old", refresh_token="synthetic-refresh-old"
        ),
    )

    def save_tokens(access_token: str, refresh_token: str) -> auth.JwtCredential:
        saved_tokens.append((access_token, refresh_token))
        return auth.JwtCredential(
            access_token=access_token, refresh_token=refresh_token
        )

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    def client(*, api_url: str, credential: auth.ResolvedCredential) -> SolwynClient:
        return SolwynClient(
            api_url=api_url,
            credential=credential,
            transport=httpx.MockTransport(handle),
            sleeper=sleeps.append,
        )

    monkeypatch.setattr(auth, "store_jwt_pair", save_tokens)
    monkeypatch.setattr(common, "SolwynClient", client)
    return responses, calls, sleeps, saved_tokens


def invoke(args: list[str], *, input: str | None = None):
    return runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_one",
            "--api-url",
            "https://api.example.com",
            *args,
        ],
        input=input,
    )


@pytest.mark.parametrize(
    "args,status,payload",
    [
        (["channels", "delete", "--yes", "ch_one"], 204, None),
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
            201,
            CHANNEL,
        ),
        (["channels", "update", "ch_one", "--name", "Ops"], 200, CHANNEL),
        (
            ["channels", "test", "ch_one"],
            200,
            {"success": True, "message": "Test sent"},
        ),
        (["notifications", "set", "--no-sms"], 200, CONFIG),
        (["notifications", "test", "sms"], 200, SMS_RESULT),
        (["account", "alert-email", "set", "ops@example.com"], 200, ALERT_EMAIL),
        (["account", "alert-email", "clear"], 200, ALERT_EMAIL),
    ],
)
def test_mutations_retry_429_once_after_retry_after(
    transport_api, args, status, payload
):
    responses, calls, sleeps, saved_tokens = transport_api
    responses.extend(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"detail": "Rate limit exceeded"},
            ),
            httpx.Response(status, **({} if status == 204 else {"json": payload})),
        ]
    )
    result = invoke(args, input="synthetic-secret-abcd\n")
    assert result.exit_code == 0, result.output
    assert sleeps == [2.0]
    assert len(calls) == 2
    assert calls[0].method == calls[1].method
    assert calls[0].url == calls[1].url
    assert calls[0].content == calls[1].content
    assert saved_tokens == []


def test_mutation_exhausted_429_stops_after_one_retry(transport_api):
    responses, calls, sleeps, _ = transport_api
    responses.extend(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"detail": "Rate limit exceeded"},
            )
            for _ in range(2)
        ]
    )
    result = invoke(["channels", "delete", "--yes", "ch_one"])
    assert result.exit_code == 1
    assert len(calls) == 2
    assert sleeps == [2.0]
    assert "rate_limited" in result.stderr


@pytest.mark.parametrize(
    "args,status,payload",
    [
        (["channels", "show", "ch_one"], 200, CHANNEL),
        (["channels", "delete", "--yes", "ch_one"], 204, None),
        (["notifications", "set", "--no-sms"], 200, CONFIG),
    ],
)
def test_notification_routes_keep_one_jwt_refresh(transport_api, args, status, payload):
    responses, calls, sleeps, saved_tokens = transport_api
    responses.extend(
        [
            httpx.Response(401, json={"detail": "Invalid token"}),
            httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-new",
                    "refresh_token": "synthetic-refresh-new",
                    "token_type": "bearer",
                },
            ),
            httpx.Response(status, **({} if status == 204 else {"json": payload})),
        ]
    )
    result = invoke(args)
    assert result.exit_code == 0, result.output
    assert len(calls) == 3
    assert calls[1].url.path == "/api/v1/auth/refresh"
    assert calls[0].headers["Authorization"] == "Bearer synthetic-access-old"
    assert calls[2].headers["Authorization"] == "Bearer synthetic-access-new"
    assert calls[0].method == calls[2].method
    assert calls[0].content == calls[2].content
    assert saved_tokens == [("synthetic-access-new", "synthetic-refresh-new")]
    assert sleeps == []
    assert "synthetic-access" not in result.output
    assert "synthetic-refresh" not in result.output


def test_unsupported_clear_config_not_advertised_or_accepted(transport_api):
    _, calls, _, _ = transport_api
    help_result = runner.invoke(app, ["channels", "update", "--help"])
    assert "--clear-config" not in help_result.stdout
    assert "remains usable" in " ".join(help_result.stdout.split())
    result = invoke(["channels", "update", "ch_one", "--clear-config"])
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize(
    "kind,config,message",
    [
        (
            "slack",
            {"webhook_url": None, "bot_token": None},
            "slack channels require a webhook URL or a bot token plus channel.",
        ),
        (
            "pagerduty",
            {"routing_key": None},
            "pagerduty channels require a routing key.",
        ),
        ("teams", {"webhook_url": None}, "teams channels require a webhook URL."),
        ("discord", {"webhook_url": None}, "discord channels require a webhook URL."),
        (
            "telegram",
            {"bot_token": None},
            "telegram channels require a bot token and chat id.",
        ),
    ],
)
def test_unusable_null_updates_surface_core_constraint(
    transport_api, kind, config, message
):
    # Core's _validate_update_payload validates merged credentials BEFORE writes
    # (account_notification_channels.py:471-507,1052). Preserve its 422 response.
    responses, calls, sleeps, _ = transport_api
    stored_config = {
        "slack": {"webhook_url": "****abcd"},
        "pagerduty": {"routing_key": "****abcd"},
        "teams": {"webhook_url": "****abcd"},
        "discord": {"webhook_url": "****abcd"},
        "telegram": {"bot_token": "****abcd", "chat_id": "-123"},
    }[kind]
    existing = {
        **CHANNEL,
        "channel_type": kind,
        "slack_config": None,
        f"{kind}_config": stored_config,
    }
    responses.extend(
        [
            httpx.Response(200, json=existing),
            httpx.Response(
                422,
                json={
                    "detail": {
                        "code": "invalid_notification_channel_config",
                        "message": message,
                    }
                },
            ),
        ]
    )
    result = invoke(
        ["channels", "update", "ch_one", "--config-stdin"], input=json.dumps(config)
    )
    assert result.exit_code == 1
    assert len(calls) == 2
    assert json.loads(calls[1].content) == {f"{kind}_config": config}
    assert "invalid_notification_channel_config" in result.stderr
    assert message in result.stderr
    assert sleeps == []


def test_sms_help_and_output_describe_eligibility_only(transport_api):
    responses, calls, _, _ = transport_api
    help_result = runner.invoke(app, ["notifications", "test", "--help"])
    assert "SMS checks eligibility only" in " ".join(help_result.stdout.split())
    responses.append(httpx.Response(200, json=SMS_RESULT))
    result = invoke(["notifications", "test", "sms"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["message"] == SMS_RESULT["message"]
    assert len(calls) == 1


def test_rejected_refreshed_jwt_stops_after_one_refresh(transport_api):
    responses, calls, sleeps, saved_tokens = transport_api
    responses.extend(
        [
            httpx.Response(401, json={"detail": "Invalid token"}),
            httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-new",
                    "refresh_token": "synthetic-refresh-new",
                    "token_type": "bearer",
                },
            ),
            httpx.Response(401, json={"detail": "Invalid token"}),
        ]
    )
    result = invoke(["channels", "delete", "--yes", "ch_one"])
    assert result.exit_code == 3
    assert [call.method for call in calls] == ["DELETE", "POST", "DELETE"]
    assert len(saved_tokens) == 1
    assert sleeps == []


def test_mutation_503_is_not_replayed(transport_api):
    responses, calls, sleeps, saved_tokens = transport_api
    responses.append(httpx.Response(503, json={"detail": "Provider unavailable"}))
    result = invoke(["channels", "test", "ch_one"])
    assert result.exit_code == 1
    assert len(calls) == 1
    assert sleeps == []
    assert saved_tokens == []
