"""Webhook target command contract tests."""

import importlib
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "d4" * 32
ACCESS_TOKEN = "webhooks-access-token"
REFRESH_TOKEN = "webhooks-refresh-token"
PROJECT_ID = "proj/webhooks"
WEBHOOK_ID = "wh/alpha"
SIGNING_SECRET = "ab" * 32
ROTATED_SECRET = "cd" * 32

WEBHOOK_RESPONSE = {
    "id": WEBHOOK_ID,
    "url": "https://hooks.example.com/solwyn",
    "events": "budget_threshold,future.delivery_ready",
    "is_active": True,
    "secret_prefix": "a1b2c3d4...",
    "created_at": "2026-08-31T14:30:00Z",
}
CREATE_RESPONSE = {
    **WEBHOOK_RESPONSE,
    "secret_prefix": SIGNING_SECRET[:8] + "...",
    "signing_secret": SIGNING_SECRET,
}
ROTATE_RESPONSE = {
    "id": WEBHOOK_ID,
    "key_version": 2,
    "signing_secret": ROTATED_SECRET,
    "secret_prefix": ROTATED_SECRET[:8] + "...",
}


class FakeClient:
    instances: list["FakeClient"] = []
    responses: list[httpx.Response | BaseException] = []

    def __init__(self, *, api_url: str, credential: object = None) -> None:
        self.api_url = api_url
        self.credential = credential
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, path, kwargs))
        result = self.__class__.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    module = sys.modules.get("solwyn_cli.commands.webhooks")
    if module is not None:
        monkeypatch.setattr(module, "SolwynClient", FakeClient)
    yield


def _jwt() -> auth.JwtCredential:
    return auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )


def _key(source: str) -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _module() -> Any:
    return importlib.import_module("solwyn_cli.commands.webhooks")


def _options(output_format: str) -> list[str]:
    return ["--json"] if output_format == "json" else ["--no-color"]


def test_webhooks_group_registers_exact_surface() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["webhooks", "--help"])

    assert root.exit_code == 0
    assert "webhooks" in root.stdout
    assert group.exit_code == 0
    registered = get_command(app).commands["webhooks"].commands
    assert set(registered) == {
        "list",
        "create",
        "rotate",
        "delete",
    }


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_webhooks_list_is_jwt_only_quotes_project_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", lambda: credential)
    FakeClient.responses = [httpx.Response(200, json=[WEBHOOK_RESPONSE])]

    result = runner.invoke(
        app,
        [*_options(output_format), "--project", PROJECT_ID, "webhooks", "list"],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj%2Fwebhooks/webhooks", {})
    ]
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert SIGNING_SECRET not in rendered
    assert "a1b2c3d4..." in result.stdout
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"webhooks-list-{output_format}.{suffix}"
    )
    assert result.stderr == ""


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_webhooks_create_posts_open_event_string_and_reveals_secret_once(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = _jwt()
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        httpx.Response(201, json=CREATE_RESPONSE),
        RuntimeError("must not retry"),
    ]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            [
                *_options(output_format),
                "--project",
                PROJECT_ID,
                "webhooks",
                "create",
                "--url",
                "https://hooks.example.com/solwyn",
                "--events",
                "budget_threshold,future.delivery_ready",
            ],
        )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            "/api/v1/projects/proj%2Fwebhooks/webhooks",
            {
                "json": {
                    "url": "https://hooks.example.com/solwyn",
                    "events": "budget_threshold,future.delivery_ready",
                },
                "retry_rate_limit": False,
                "retry_auth": False,
            },
        )
    ]
    rendered = result.stdout + result.stderr + repr(result.exception) + caplog.text
    assert rendered.count(SIGNING_SECRET) == 1
    assert result.stderr == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"webhooks-create-{output_format}.{suffix}"
    )


def test_webhooks_create_uses_star_default_without_closing_event_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(
            201,
            json={**CREATE_RESPONSE, "events": "*"},
        )
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj",
            "webhooks",
            "create",
            "--url",
            "https://hooks.example.com/default",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["json"] == {
        "url": "https://hooks.example.com/default",
        "events": "*",
    }


def test_webhooks_create_relays_free_tier_402_before_local_ssrf_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    detail = {
        "code": "upgrade_required",
        "feature": "webhook_targets.count",
        "current_tier": "free",
        "required_tier": "production",
        "message": "Webhook targets require the Production tier.",
        "allowed_values": ["production", "team", "scale"],
        "cta_path": "/billing",
    }
    FakeClient.responses = [
        ApiError(
            status_code=402,
            code="upgrade_required",
            message=detail["message"],
            hint=(
                "Allowed values: production, team, scale. "
                "Review upgrade options in the dashboard: /billing"
            ),
            detail=detail,
        )
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj",
            "webhooks",
            "create",
            "--url",
            "http://localhost/private",
        ],
    )

    assert result.exit_code == 1
    assert FakeClient.instances[0].requests[0][2]["json"]["url"] == (
        "http://localhost/private"
    )
    payload = json.loads(result.stderr)
    assert payload["message"] == detail["message"]
    assert payload["detail"] == detail
    assert payload["hint"].endswith("dashboard: /billing")


def test_webhooks_create_relays_core_ssrf_400_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    message = "Webhook URL must not target private or reserved IP addresses"
    FakeClient.responses = [
        ApiError(
            status_code=400,
            code="http_error",
            message=message,
            hint=None,
            detail=message,
        )
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj",
            "webhooks",
            "create",
            "--url",
            "https://127.0.0.1/hook",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["message"] == message
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_webhooks_rotate_quotes_ids_posts_no_body_and_reveals_secret_once(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = _jwt()
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        httpx.Response(200, json=ROTATE_RESPONSE),
        RuntimeError("must not retry"),
    ]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            [
                *_options(output_format),
                "--project",
                PROJECT_ID,
                "webhooks",
                "rotate",
                WEBHOOK_ID,
            ],
        )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            "/api/v1/projects/proj%2Fwebhooks/webhooks/wh%2Falpha/rotate",
            {"retry_rate_limit": False, "retry_auth": False},
        )
    ]
    rendered = result.stdout + result.stderr + repr(result.exception) + caplog.text
    assert rendered.count(ROTATED_SECRET) == 1
    assert result.stderr == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"webhooks-rotate-{output_format}.{suffix}"
    )


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_webhooks_delete_requires_bodyless_204_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(204),
        RuntimeError("must not retry"),
    ]

    result = runner.invoke(
        app,
        [
            *_options(output_format),
            "--project",
            PROJECT_ID,
            "webhooks",
            "delete",
            WEBHOOK_ID,
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "DELETE",
            "/api/v1/projects/proj%2Fwebhooks/webhooks/wh%2Falpha",
            {"retry_rate_limit": False, "retry_auth": False},
        )
    ]
    assert result.stderr == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"webhooks-delete-{output_format}.{suffix}"
    )


@pytest.mark.parametrize(
    "command",
    [
        ["list"],
        ["create", "--url", "https://hooks.example.com/hook"],
        ["rotate", WEBHOOK_ID],
        ["delete", WEBHOOK_ID],
    ],
)
@pytest.mark.parametrize(
    ("source", "expected_hint"),
    [
        (
            "environment",
            "Unset SOLWYN_API_KEY, run solwyn login, then retry webhooks",
        ),
        (
            "keyring",
            "Run solwyn logout, then solwyn login, and retry webhooks",
        ),
    ],
)
def test_webhooks_commands_are_jwt_only_without_network(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    source: str,
    expected_hint: str,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", lambda: _key(source))

    result = runner.invoke(
        app,
        ["--json", "--project", "proj", "webhooks", *command],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert expected_hint in json.loads(result.stderr)["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "command",
    [
        ["list"],
        ["create", "--url", "https://hooks.example.com/hook"],
        ["rotate", "wh"],
        ["delete", "wh"],
    ],
)
def test_webhooks_commands_require_login_without_network(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", lambda: None)

    result = runner.invoke(
        app,
        ["--json", "--project", "proj", "webhooks", *command],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "authentication_required"
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        ["--project", "   ", "webhooks", "list"],
        ["--project", "proj", "webhooks", "rotate", "   "],
        ["--project", "proj", "webhooks", "delete", "   "],
        ["--project", "proj", "webhooks", "create", "--url", "not-a-url"],
    ],
)
def test_webhooks_validate_local_identifiers_and_uri_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "resolve_credential",
        lambda: pytest.fail("must validate before auth"),
    )

    result = runner.invoke(app, ["--json", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    ("command", "response"),
    [
        (["list"], httpx.Response(201, json=[WEBHOOK_RESPONSE])),
        (["list"], httpx.Response(200, json={"webhooks": [WEBHOOK_RESPONSE]})),
        (
            ["list"],
            httpx.Response(
                200,
                json=[{**WEBHOOK_RESPONSE, "signing_secret": SIGNING_SECRET}],
            ),
        ),
        (
            ["create", "--url", "https://hooks.example.com/hook"],
            httpx.Response(200, json=CREATE_RESPONSE),
        ),
        (
            ["create", "--url", "https://hooks.example.com/hook"],
            httpx.Response(
                201,
                json={
                    key: value
                    for key, value in CREATE_RESPONSE.items()
                    if key != "signing_secret"
                },
            ),
        ),
        (["rotate", "wh"], httpx.Response(201, json=ROTATE_RESPONSE)),
        (
            ["rotate", "wh"],
            httpx.Response(
                200,
                json={**ROTATE_RESPONSE, "signing_secret": "not-hex"},
            ),
        ),
        (["delete", "wh"], httpx.Response(200, json={})),
        (["delete", "wh"], httpx.Response(204, content=b"{}")),
    ],
)
def test_webhooks_reject_malformed_or_wrong_status_responses_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    response: httpx.Response,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    FakeClient.responses = [response]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            ["--json", "--project", "proj", "webhooks", *command],
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_webhooks_response"
    rendered = result.stderr + repr(result.exception) + caplog.text
    assert SIGNING_SECRET not in rendered
    assert ROTATED_SECRET not in rendered
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        (["list"], [{**WEBHOOK_RESPONSE, "server_note": "new metadata"}]),
        (
            ["create", "--url", "https://hooks.example.com/hook"],
            {**CREATE_RESPONSE, "server_note": "new metadata"},
        ),
        (["rotate", "wh"], {**ROTATE_RESPONSE, "server_note": "new metadata"}),
    ],
)
def test_webhooks_ignore_benign_additive_response_fields(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    payload: object,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    status = 201 if command[0] == "create" else 200
    FakeClient.responses = [httpx.Response(status, json=payload)]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj", "webhooks", *command],
    )

    assert result.exit_code == 0, result.stderr
    assert "server_note" not in result.stdout + result.stderr


def test_webhooks_not_found_and_deactivated_errors_are_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve_credential", _jwt)
    FakeClient.responses = [
        ApiError(
            status_code=404,
            code="http_error",
            message="Webhook not found",
            hint=None,
            detail="Webhook not found",
        ),
        ApiError(
            status_code=410,
            code="http_error",
            message="Webhook is deactivated",
            hint=None,
            detail="Webhook is deactivated",
        ),
    ]

    missing = runner.invoke(
        app,
        ["--json", "--project", "proj", "webhooks", "delete", "missing"],
    )
    inactive = runner.invoke(
        app,
        ["--json", "--project", "proj", "webhooks", "rotate", "inactive"],
    )

    assert missing.exit_code == 5
    assert (
        "Verify the project or webhook identifier" in json.loads(missing.stderr)["hint"]
    )
    assert inactive.exit_code == 1
    assert "Create a new webhook target" in json.loads(inactive.stderr)["hint"]


def test_webhook_secret_occurs_only_in_create_and_rotate_goldens() -> None:
    goldens = sorted(GOLDEN_DIR.glob("webhooks-*"))
    secret_goldens = [
        path
        for path in goldens
        if "webhooks-create-" in path.name or "webhooks-rotate-" in path.name
    ]

    assert len(secret_goldens) == 4
    assert all(
        sum(
            path.read_text(encoding="utf-8").count(secret)
            for secret in (SIGNING_SECRET, ROTATED_SECRET)
        )
        == 1
        for path in secret_goldens
    )
    assert all(
        SIGNING_SECRET not in path.read_text(encoding="utf-8")
        and ROTATED_SECRET not in path.read_text(encoding="utf-8")
        for path in goldens
        if path not in secret_goldens
    )


def test_webhook_request_and_response_contracts_are_registered() -> None:
    request_drift = importlib.import_module("test_request_drift")
    response_subset = importlib.import_module("test_response_subset")
    webhook_module = _module()

    assert request_drift.REQUEST_MIRRORS[webhook_module.WebhookCreateRequest] == (
        "WebhookCreateRequest"
    )
    assert response_subset.RESPONSE_READS["WebhookResponse"] == (
        "id",
        "url",
        "events",
        "is_active",
        "secret_prefix",
        "created_at",
    )
    assert response_subset.RESPONSE_READS["WebhookCreateResponse"] == (
        "id",
        "url",
        "events",
        "is_active",
        "secret_prefix",
        "created_at",
        "signing_secret",
    )
    assert response_subset.RESPONSE_READS["WebhookRotateResponse"] == (
        "id",
        "key_version",
        "signing_secret",
        "secret_prefix",
    )
    assert response_subset.OPERATION_RESPONSE_BINDINGS[
        ("GET", "/api/v1/projects/{project_id}/webhooks")
    ] == ("200", "array", "WebhookResponse")
    assert response_subset.OPERATION_RESPONSE_BINDINGS[
        ("POST", "/api/v1/projects/{project_id}/webhooks")
    ] == ("201", "object", "WebhookCreateResponse")
    assert response_subset.OPERATION_RESPONSE_BINDINGS[
        (
            "POST",
            "/api/v1/projects/{project_id}/webhooks/{webhook_id}/rotate",
        )
    ] == ("200", "object", "WebhookRotateResponse")
    assert (
        response_subset.NO_CONTENT_OPERATION_BINDINGS[
            ("DELETE", "/api/v1/projects/{project_id}/webhooks/{webhook_id}")
        ]
        == "204"
    )
