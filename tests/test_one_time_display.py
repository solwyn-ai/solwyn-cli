"""Only the newly minted key escapes redaction in a one-time display (review
round 1 item 5): every other secret-shaped value next to it is redacted."""

import io
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from key_fixtures import KEY
from login_fixtures import LOGIN_TOKEN, login_credential
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.main import app
from solwyn_cli.output import OneTimeSecretPanel, OutputEngine, OutputFormat, TableData
from test_projects_command import PROJECT_CREATE_RESPONSE

runner = CliRunner()
NEW_KEY = PROJECT_CREATE_RESPONSE["key"]


class Client:
    def __init__(self, **_: Any) -> None:
        pass

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, path: str, **_: Any) -> httpx.Response:
        return httpx.Response(
            201, json={**PROJECT_CREATE_RESPONSE, "name": LOGIN_TOKEN}
        )


@pytest.fixture(autouse=True)
def created_with_an_echoed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SOLWYN_PROJECT", "SOLWYN_TOKEN", "SOLWYN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(projects_module, "SolwynClient", Client)
    monkeypatch.setattr(
        projects_module, "resolve_credential", lambda **_: login_credential()
    )


@pytest.mark.parametrize("flag", ["--no-color", "--json"])
def test_a_create_response_echoing_a_login_token_never_prints_it(flag: str) -> None:
    result = runner.invoke(
        app,
        [flag, "projects", "create", "--name", "Created project"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.output
    assert LOGIN_TOKEN not in result.output
    assert NEW_KEY in result.stdout
    if flag == "--json":
        assert json.loads(result.stdout)["key"] == NEW_KEY
    else:
        assert "export SOLWYN_PROJECT='[REDACTED]'" in result.stdout


def _engine(output_format: OutputFormat) -> tuple[OutputEngine, io.StringIO]:
    stdout = io.StringIO()
    engine = OutputEngine(
        output_format=output_format,
        stdout=stdout,
        stderr=io.StringIO(),
        stdout_is_tty=False,
        stderr_is_tty=False,
        no_color=True,
        environ={},
    )
    return engine, stdout


def test_revealed_json_fields_keep_only_the_new_key() -> None:
    engine, stdout = _engine(OutputFormat.JSON)
    other = "solwyn_sk_" + "9f" * 32

    engine.write_success(
        schema="solwyn.test.v1",
        data={"ok": True},
        reveal={
            "key": SecretStr(KEY),
            "export": SecretStr(f"export SOLWYN_API_KEY='{KEY}'\nexport X='{other}'"),
        },
        exempt=SecretStr(KEY),
    )

    payload = json.loads(stdout.getvalue())
    assert payload["key"] == KEY
    assert KEY in payload["export"]
    assert other not in payload["export"]
    assert "[REDACTED]" in payload["export"]


def test_a_panel_footer_redacts_everything_but_the_panels_key() -> None:
    engine, stdout = _engine(OutputFormat.TABLE)

    engine.write_success(
        schema="solwyn.test.v1",
        data={"ok": True},
        table=TableData(columns=("Field", "Value"), rows=(("ok", "yes"),)),
        one_time_secret=OneTimeSecretPanel(
            title="One-time API key",
            value=KEY,
            footer=f"export SOLWYN_API_KEY='{KEY}'\nexport SOLWYN_PROJECT='{LOGIN_TOKEN}'",
        ),
    )

    assert f"export SOLWYN_API_KEY='{KEY}'" in stdout.getvalue()
    assert LOGIN_TOKEN not in stdout.getvalue()
