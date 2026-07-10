"""Output format, machine-envelope, and human rendering contract tests."""

import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from pydantic import BaseModel

from solwyn_cli.errors import ApiError
from solwyn_cli.output import (
    OutputEngine,
    OutputError,
    OutputFormat,
    OutputUsageError,
    TableData,
    color_enabled,
    resolve_output_format,
)


GOLDEN_DIR = Path(__file__).parent / "golden"


class ResponseModel(BaseModel):
    project: str
    observed_at: datetime


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


def _readable_ansi(value: str) -> str:
    return value.replace("\x1b", "<ESC>")


def _engine(
    output_format: OutputFormat,
    *,
    stdout_is_tty: bool = False,
    stderr_is_tty: bool = False,
    no_color: bool = False,
    environ: dict[str, str] | None = None,
) -> tuple[OutputEngine, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    engine = OutputEngine(
        output_format=output_format,
        stdout=stdout,
        stderr=stderr,
        stdout_is_tty=stdout_is_tty,
        stderr_is_tty=stderr_is_tty,
        no_color=no_color,
        environ={} if environ is None else environ,
        width=80,
    )
    return engine, stdout, stderr


@pytest.mark.parametrize(
    ("explicit_format", "json_flag", "environ", "config_format", "expected"),
    [
        ("table", False, {"SOLWYN_FORMAT": "json"}, "json", OutputFormat.TABLE),
        ("json", False, {"SOLWYN_FORMAT": "table"}, "table", OutputFormat.JSON),
        (None, True, {"SOLWYN_FORMAT": "table"}, "table", OutputFormat.JSON),
        (None, False, {"SOLWYN_FORMAT": "json"}, "table", OutputFormat.JSON),
        (None, False, {}, "json", OutputFormat.JSON),
        (None, False, {}, None, OutputFormat.TABLE),
    ],
)
def test_format_precedence_is_explicit_then_env_then_config_then_table(
    explicit_format: str | None,
    json_flag: bool,
    environ: dict[str, str],
    config_format: str | None,
    expected: OutputFormat,
) -> None:
    assert (
        resolve_output_format(
            explicit_format=explicit_format,
            json_flag=json_flag,
            environ=environ,
            config_format=config_format,
        )
        is expected
    )


def test_non_tty_does_not_auto_switch_table_to_json() -> None:
    assert (
        resolve_output_format(
            explicit_format=None,
            json_flag=False,
            environ={},
            config_format=None,
        )
        is OutputFormat.TABLE
    )


def test_conflicting_explicit_json_and_table_is_a_usage_error() -> None:
    with pytest.raises(OutputUsageError, match="Conflicting output format options"):
        resolve_output_format(
            explicit_format="table",
            json_flag=True,
            environ={},
            config_format="table",
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "explicit_format": "yaml",
                "json_flag": False,
                "environ": {},
                "config_format": "table",
            },
            "Invalid explicit output format",
        ),
        (
            {
                "explicit_format": None,
                "json_flag": False,
                "environ": {"SOLWYN_FORMAT": ""},
                "config_format": "table",
            },
            "Invalid SOLWYN_FORMAT",
        ),
        (
            {
                "explicit_format": None,
                "json_flag": False,
                "environ": {},
                "config_format": "yaml",
            },
            "Invalid configured output format",
        ),
    ],
)
def test_invalid_format_sources_fail_safely(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(OutputUsageError, match=match) as exc_info:
        resolve_output_format(**kwargs)  # type: ignore[arg-type]

    assert "yaml" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("is_tty", "no_color", "environ", "expected"),
    [
        (True, False, {}, True),
        (False, False, {}, False),
        (True, True, {}, False),
        (True, False, {"NO_COLOR": "1"}, False),
        (True, False, {"NO_COLOR": ""}, False),
    ],
)
def test_color_requires_tty_and_respects_both_disable_controls(
    is_tty: bool,
    no_color: bool,
    environ: dict[str, str],
    expected: bool,
) -> None:
    assert color_enabled(is_tty=is_tty, no_color=no_color, environ=environ) is expected


def test_success_json_matches_golden_and_accepts_pydantic_payload() -> None:
    engine, stdout, stderr = _engine(OutputFormat.JSON, stdout_is_tty=True)
    response = ResponseModel(
        project="proj_alpha",
        observed_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
    )

    engine.write_success(schema="solwyn.costs.summary.v1", data=response)

    assert stdout.getvalue() == _golden("success.json")
    assert stderr.getvalue() == ""
    assert "\x1b" not in stdout.getvalue()


def test_success_schema_is_validated_and_cannot_be_overridden() -> None:
    engine, stdout, stderr = _engine(OutputFormat.JSON)

    with pytest.raises(OutputUsageError, match="Invalid command schema"):
        engine.write_success(schema="costs.summary.v1", data={"cost": "12.34"})
    with pytest.raises(OutputUsageError, match="reserved schema field"):
        engine.write_success(
            schema="solwyn.costs.summary.v1",
            data={"schema": "solwyn.evil.v1", "cost": "12.34"},
        )

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_error_json_matches_golden_with_exact_keys_and_nested_detail() -> None:
    engine, stdout, stderr = _engine(OutputFormat.JSON, stderr_is_tty=True)
    error = ApiError(
        status_code=402,
        code="quota_exceeded",
        message="Monthly analytics quota exceeded.",
        hint="Review usage in the dashboard.",
        detail={"limit": 5000, "used": 5001, "period": {"month": "2026-07"}},
    )

    engine.write_error(error)

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == _golden("error.json")
    assert set(json.loads(stderr.getvalue())) == {
        "schema",
        "code",
        "message",
        "hint",
        "detail",
    }
    assert "\x1b" not in stderr.getvalue()


def test_safe_local_error_can_be_rendered_as_machine_json() -> None:
    engine, stdout, stderr = _engine(OutputFormat.JSON)

    engine.write_error(OutputError(code="invalid_project", message="Choose a project."))

    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "schema": "solwyn.error.v1",
        "code": "invalid_project",
        "message": "Choose a project.",
        "hint": None,
        "detail": None,
    }


def _table() -> TableData:
    return TableData(
        columns=("Project", "Cost"),
        rows=(("alpha", "$12.34"),),
        right_align=("Cost",),
    )


@pytest.mark.parametrize(
    ("stdout_is_tty", "no_color", "environ", "golden_name"),
    [
        (True, False, {}, "table-tty.txt"),
        (True, True, {}, "table-no-color.txt"),
        (False, False, {}, "table-non-tty.txt"),
        (True, False, {"NO_COLOR": ""}, "table-no-color.txt"),
    ],
)
def test_human_table_matches_tty_and_color_goldens(
    stdout_is_tty: bool,
    no_color: bool,
    environ: dict[str, str],
    golden_name: str,
) -> None:
    engine, stdout, stderr = _engine(
        OutputFormat.TABLE,
        stdout_is_tty=stdout_is_tty,
        no_color=no_color,
        environ=environ,
    )

    engine.write_success(
        schema="solwyn.costs.summary.v1",
        data={"cost": "12.34"},
        table=_table(),
    )

    assert _readable_ansi(stdout.getvalue()) == _golden(golden_name)
    assert stderr.getvalue() == ""


def test_human_error_and_hint_match_golden_without_dumping_detail() -> None:
    engine, stdout, stderr = _engine(
        OutputFormat.TABLE,
        stderr_is_tty=False,
    )
    error = OutputError(
        code="quota_exceeded",
        message="Monthly analytics quota exceeded.",
        hint="Review usage in the dashboard.",
        detail={"provider_key": "never-render-this"},
    )

    engine.write_error(error)

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == _golden("human-error.txt")
    assert "never-render-this" not in stderr.getvalue()


def test_human_table_neutralizes_terminal_controls_and_rich_markup() -> None:
    attack = "[bold red]literal[/bold red]\x1b]8;;x\x07link\x1b]8;;\x07\rreplace\b\x9b"
    engine, stdout, stderr = _engine(OutputFormat.TABLE, stdout_is_tty=True)

    engine.write_success(
        schema="solwyn.costs.summary.v1",
        data={"value": attack},
        table=TableData(columns=(attack,), rows=((attack,),)),
    )

    rendered = stdout.getvalue()
    assert stderr.getvalue() == ""
    assert "[bold red]literal[/bold red]" in rendered
    assert "\x1b[" in rendered  # CLI-owned Rich styling remains enabled.
    assert "\x1b]" not in rendered
    assert "\x07" not in rendered
    assert "\r" not in rendered
    assert "\b" not in rendered
    assert "\x9b" not in rendered


def test_human_error_fields_neutralize_controls_without_parsing_markup() -> None:
    attack = (
        "[link=https://attacker.example]literal[/link]"
        "\x1b]8;;https://attacker.example\x07link\x1b]8;;\x07"
        "\rreplace\b\x9bcontrol"
    )
    engine, stdout, stderr = _engine(OutputFormat.TABLE, stderr_is_tty=True)

    engine.write_error(OutputError(code=attack, message=attack, hint=attack))

    rendered = stderr.getvalue()
    assert stdout.getvalue() == ""
    assert "[link=https://attacker.example]literal[/link]" in rendered
    assert "\x1b[" in rendered  # CLI-owned Rich styling remains enabled.
    assert "\x1b]" not in rendered
    assert "\x07" not in rendered
    assert "\r" not in rendered
    assert "\b" not in rendered
    assert "\x9b" not in rendered


def test_json_escapes_terminal_controls_but_preserves_machine_data() -> None:
    attack = "escape\x1b osc\x9b bell\x07 return\r backspace\b"
    engine, stdout, stderr = _engine(OutputFormat.JSON)

    engine.write_success(
        schema="solwyn.costs.summary.v1",
        data={"value": attack},
    )
    engine.write_error(OutputError(code="unsafe", message=attack, hint=attack))

    assert json.loads(stdout.getvalue())["value"] == attack
    assert json.loads(stderr.getvalue())["message"] == attack
    for rendered in (stdout.getvalue(), stderr.getvalue()):
        assert "\x1b" not in rendered
        assert "\x9b" not in rendered
        assert "\x07" not in rendered
        assert "\r" not in rendered
        assert "\b" not in rendered


def test_table_shape_is_validated_before_rendering() -> None:
    with pytest.raises(ValueError, match="same number of cells"):
        TableData(columns=("Project", "Cost"), rows=(("alpha",),))
