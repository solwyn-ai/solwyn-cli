"""CLI tokens and device codes are redacted from everything the CLI renders.

Human and JSON output, error messages, prompts and MCP results (spec section
4). Raw exports (costs and alerts) are byte-for-byte copies of server data and are
not covered.
"""

import ast
import functools
import json
from io import StringIO
from pathlib import Path

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


@pytest.mark.parametrize("secret", [LEAKED_TOKEN, DEVICE_CODE])
def test_mcp_results_redact_tokens_in_text_and_structured_content(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    from test_mcp_read_gaps import call

    project = {
        "id": "proj_alpha",
        "name": f"name_{secret}_suffix",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "hard_deny",
        "created_at": "2026-07-09T14:30:00Z",
    }

    result, _ = call(
        monkeypatch, "list_projects", [httpx.Response(200, json=[project])]
    )

    assert not result.is_error, result.structured_content
    text = result.content[0].text
    assert secret not in text
    assert secret not in json.dumps(result.structured_content)
    assert json.loads(text) == result.structured_content
    names = [item["name"] for item in result.structured_content["data"]["projects"]]
    assert names == ["name_[REDACTED]_suffix"]


def test_mcp_error_results_redact_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_mcp_read_gaps import call

    result, _ = call(
        monkeypatch,
        "list_projects",
        [
            httpx.Response(
                401,
                json={
                    "detail": {
                        "code": "cli_credential_invalid",
                        "message": f"Unknown {LEAKED_TOKEN} for {DEVICE_CODE}.",
                    }
                },
            )
        ],
    )

    rendered = result.content[0].text + json.dumps(result.structured_content)
    assert LEAKED_TOKEN not in rendered
    assert DEVICE_CODE not in rendered


# Direct terminal writes that interpolate a value must pass it through the
# redactor. Each exemption below carries only constants, numbers, dates, local
# config, the version, or text already sanitized.
_DIRECT_WRITES = {"typer.echo", "typer.prompt", "typer.confirm", "click.echo"}
_REDACTORS = {"_terminal_safe", "redact_secrets"}
_EXEMPT_WRITES = {
    ("_record.py", "f'Warning: {INGEST_BUSY_HINT}'"),
    ("_tags.py", "f'Duplicate tag {key!r}: last value wins.'"),
    ("agent_context.py", "render_surface(ArtifactSurface.AGENT_CONTEXT)"),
    ("budget.py", "UNCOUNTED_NOTICE"),
    ("budget.py", "_FAIL_OPEN_NOTICE"),
    ("budget_rules.py", "_RULE_NOTICE"),
    ("budget_rules.py", "_KEY_NOTICE"),
    ("config.py", "'' if value is None else value"),
    ("config.py", "f'Set {validated_key}.'"),
    ("config.py", "f\"{key}={('' if value is None else value)}\""),
    (
        "cost_analysis.py",
        "f'Drift coverage: {data.included_event_count} estimates included; {data.missing_estimate_count} events missing estimates.'",
    ),
    (
        "costs.py",
        "f'Retention clamped window: {window.effective_from} to {window.effective_to} (requested {window.requested_from} to {window.requested_to}).'",
    ),
    (
        "costs.py",
        "f'Savings: ${savings.saved_cost:.6f}; upper {upper}; blocked calls {savings.blocked_call_count}.'",
    ),
    ("init.py", "f'Project ({project_ids}, or create)'"),
    ("invite.py", "label"),
    ("keys.py", "line"),
    ("projects.py", "f\"Type the project name '{shown}' to confirm\""),
    (
        "record.py",
        "f'Recorded batch progress: {min(offset + 1000, len(events))}/{len(events)} events processed.'",
    ),
    (
        "runs.py",
        "f'window: {_date_time(window.effective_from)} to {_date_time(window.effective_to)}'",
    ),
    (
        "runs.py",
        "f'retention clamp applied: requested {_date_time(window.requested_from)} to {_date_time(window.requested_to)}'",
    ),
    (
        "runs.py",
        "f'stops future checks and lease renewals; in-flight requests finish; fully effective by {_date_time(effective_by)}'",
    ),
    ("tags.py", "f'Account tag keys: {data.usage.keys_used}/{data.usage.keys_cap}.'"),
    ("main.py", "f'solwyn {__version__}'"),
}


def _interpolates(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Call) and ast.unparse(node.func) in _REDACTORS:
        return False
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp):
        return _interpolates(node.left) or _interpolates(node.right)
    if isinstance(node, ast.IfExp):
        return _interpolates(node.body) or _interpolates(node.orelse)
    return True


def _unredacted_writes(root: Path) -> set[tuple[str, str]]:
    found = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in _DIRECT_WRITES
                and node.args
                and _interpolates(node.args[0])
            ):
                found.add((path.name, ast.unparse(node.args[0])))
    return found


def test_direct_terminal_writes_redact_interpolated_values() -> None:
    unexpected = _unredacted_writes(Path("src/solwyn_cli")) - _EXEMPT_WRITES
    assert unexpected == set(), (
        "Wrap interpolated server data in _terminal_safe, or exempt a write "
        f"that carries none: {sorted(unexpected)}"
    )
    stale = _EXEMPT_WRITES - _unredacted_writes(Path("src/solwyn_cli"))
    assert stale == set(), f"exemptions that no longer match a write: {sorted(stale)}"


def test_direct_write_guard_catches_an_unredacted_notice(tmp_path: Path) -> None:
    (tmp_path / "notice.py").write_text(
        "import typer\n"
        "def show(data):\n"
        "    typer.echo(f'Cause: {data.summary}', err=True)\n"
        "    typer.echo(_terminal_safe(f'Cause: {data.summary}'), err=True)\n"
    )

    assert _unredacted_writes(tmp_path) == {("notice.py", "f'Cause: {data.summary}'")}
