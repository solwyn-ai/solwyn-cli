"""CLI tokens and device codes never reach any output stream (spec section 4)."""

import functools
import json
from io import StringIO

import httpx
import pytest
from typer.testing import CliRunner

from login_fixtures import LOGIN_TOKEN, fake_keychain, login_entries
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import credentials as credentials_module
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.errors import ApiError
from solwyn_cli.output import OutputEngine, OutputFormat, TableData
from solwyn_cli.main import app


runner = CliRunner()
LEAKED_TOKEN = "solwyn_cli_" + "9a" * 32
DEVICE_CODE = "solwyn_dc_" + "4f" * 32
SECRETS = (LEAKED_TOKEN, DEVICE_CODE, LOGIN_TOKEN)


def _serve(monkeypatch: pytest.MonkeyPatch, module, response: httpx.Response) -> None:
    monkeypatch.setattr(
        module,
        "SolwynClient",
        functools.partial(
            SolwynClient, transport=httpx.MockTransport(lambda request: response)
        ),
    )


@pytest.mark.parametrize("output", [["--json"], ["--no-color"]])
def test_server_error_echoing_a_token_and_device_code_is_redacted(
    monkeypatch: pytest.MonkeyPatch, output: list[str]
) -> None:
    fake_keychain(monkeypatch).update(login_entries())
    _serve(
        monkeypatch,
        projects_module,
        httpx.Response(
            401,
            json={
                "detail": {
                    "code": "cli_credential_invalid",
                    "message": f"Unknown credential {LEAKED_TOKEN}.",
                    "device_code": DEVICE_CODE,
                    "nested": [{"value": f"x {LEAKED_TOKEN} y"}],
                }
            },
        ),
    )

    result = runner.invoke(app, [*output, "projects", "list"])

    assert result.exit_code == 3
    for secret in SECRETS:
        assert secret not in result.stdout + result.stderr
    assert "[REDACTED]" in result.stderr


@pytest.mark.parametrize("output", [["--json"], ["--no-color"]])
def test_success_output_echoing_a_token_is_redacted(
    monkeypatch: pytest.MonkeyPatch, output: list[str]
) -> None:
    fake_keychain(monkeypatch).update(login_entries())
    item = {
        "id": "6c1f2a90-0000-4000-8000-000000000001",
        "label": f"box {LEAKED_TOKEN}",
        "client_id": "solwyn-cli",
        "client_name": "Solwyn CLI",
        "client_version": DEVICE_CODE,
        "scope": "read",
        "token_prefix": "solwyn_cli_9a9a9a9a",
        "created_at": "2026-10-30T14:03:11Z",
        "expires_at": "2026-11-29T14:03:11Z",
        "last_used_at": None,
        "current": True,
    }
    _serve(
        monkeypatch,
        credentials_module,
        httpx.Response(200, json={"credentials": [item]}),
    )

    result = runner.invoke(app, [*output, "credentials", "list"])

    assert result.exit_code == 0, result.stderr
    for secret in SECRETS:
        assert secret not in result.stdout + result.stderr


def test_engine_redacts_every_field_of_errors_and_success() -> None:
    stdout, stderr = StringIO(), StringIO()
    engine = OutputEngine(
        output_format=OutputFormat.JSON,
        stdout=stdout,
        stderr=stderr,
        stdout_is_tty=False,
        stderr_is_tty=False,
        no_color=True,
        environ={},
    )
    engine.write_success(
        schema="solwyn.test.v1",
        data={"a": [LEAKED_TOKEN, {"b": DEVICE_CODE}]},
        table=TableData(columns=("x",), rows=((LEAKED_TOKEN,),)),
    )
    engine.write_error(
        ApiError(
            status_code=401,
            code=LEAKED_TOKEN,
            message=LEAKED_TOKEN,
            hint=DEVICE_CODE,
            detail={"k": [DEVICE_CODE]},
        )
    )

    rendered = stdout.getvalue() + stderr.getvalue()
    for secret in (LEAKED_TOKEN, DEVICE_CODE):
        assert secret not in rendered
    assert json.loads(stdout.getvalue())["a"] == ["[REDACTED]", {"b": "[REDACTED]"}]


def test_engine_redacts_human_tables_and_errors() -> None:
    stdout, stderr = StringIO(), StringIO()
    engine = OutputEngine(
        output_format=OutputFormat.TABLE,
        stdout=stdout,
        stderr=stderr,
        stdout_is_tty=False,
        stderr_is_tty=False,
        no_color=True,
        environ={},
    )
    engine.write_success(
        schema="solwyn.test.v1",
        data={},
        table=TableData(columns=("Value",), rows=((LEAKED_TOKEN,), (DEVICE_CODE,))),
    )
    engine.write_error(
        ApiError(
            status_code=401,
            code="cli_credential_invalid",
            message=f"Unknown {LEAKED_TOKEN}",
            hint=f"Device {DEVICE_CODE}",
            detail=None,
        )
    )

    rendered = stdout.getvalue() + stderr.getvalue()
    for secret in (LEAKED_TOKEN, DEVICE_CODE):
        assert secret not in rendered
    assert rendered.count("[REDACTED]") == 4


@pytest.mark.parametrize(
    "text",
    [
        f"_{LEAKED_TOKEN}_",
        f"name_{DEVICE_CODE}_suffix",
        f"x{LEAKED_TOKEN}9",
        f"{DEVICE_CODE}abc",
    ],
)
def test_tokens_wrapped_in_word_characters_are_redacted(text: str) -> None:
    from solwyn_cli.errors import redact_secrets

    redacted = redact_secrets({"name": text})
    assert LEAKED_TOKEN not in json.dumps(redacted)
    assert DEVICE_CODE not in json.dumps(redacted)
