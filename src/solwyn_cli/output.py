"""Deterministic human and machine output rendering."""

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, TextIO

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    TypeAdapter,
    model_validator,
)
from rich.console import Console
from rich.table import Table
from rich.text import Text

from solwyn_cli.errors import ApiError


_COMMAND_SCHEMA = re.compile(r"^solwyn\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\.v1$")
_JSON_ADAPTER = TypeAdapter(Any)


class OutputUsageError(ValueError):
    """An output option or payload violates the public CLI contract."""


class OutputFormat(StrEnum):
    """The only supported presentation formats."""

    TABLE = "table"
    JSON = "json"


class OutputError(BaseModel):
    """Safe local error fields accepted by the output engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    hint: str | None = None
    detail: JsonValue | None = None


class TableData(BaseModel):
    """Validated human table cells supplied by a command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    right_align: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> "TableData":
        """Require non-empty columns and consistently shaped rows."""
        if not self.columns:
            raise ValueError("Tables require at least one column")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("Table column names must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Every table row must have the same number of cells")
        if not set(self.right_align).issubset(self.columns):
            raise ValueError("Right-aligned columns must exist in the table")
        return self


def _parse_format(value: str | OutputFormat, *, source: str) -> OutputFormat:
    try:
        return OutputFormat(value)
    except ValueError:
        raise OutputUsageError(f"Invalid {source} output format.") from None


def resolve_output_format(
    *,
    explicit_format: str | OutputFormat | None,
    json_flag: bool,
    environ: Mapping[str, str],
    config_format: str | OutputFormat | None,
) -> OutputFormat:
    """Resolve explicit intent, then environment, config, and the table default."""
    if json_flag and explicit_format is not None:
        parsed_explicit = _parse_format(explicit_format, source="explicit")
        if parsed_explicit is OutputFormat.TABLE:
            raise OutputUsageError("Conflicting output format options.")
        return OutputFormat.JSON
    if json_flag:
        return OutputFormat.JSON
    if explicit_format is not None:
        return _parse_format(explicit_format, source="explicit")
    if "SOLWYN_FORMAT" in environ:
        try:
            return OutputFormat(environ["SOLWYN_FORMAT"])
        except ValueError:
            raise OutputUsageError("Invalid SOLWYN_FORMAT output format.") from None
    if config_format is not None:
        return _parse_format(config_format, source="configured")
    return OutputFormat.TABLE


def color_enabled(
    *,
    is_tty: bool,
    no_color: bool,
    environ: Mapping[str, str],
) -> bool:
    """Return whether Rich may emit color for one human stream."""
    return is_tty and not no_color and "NO_COLOR" not in environ


def _json_value(value: object) -> Any:
    return _JSON_ADAPTER.dump_python(value, mode="json")


def _success_data(data: Mapping[str, object] | BaseModel) -> dict[str, Any]:
    serialized = _json_value(data)
    if not isinstance(serialized, dict):
        raise OutputUsageError("Success payload data must be an object.")
    if "schema" in serialized:
        raise OutputUsageError(
            "Success payload data contains the reserved schema field."
        )
    return serialized


class OutputEngine:
    """Write contract-safe command output to injectable streams."""

    def __init__(
        self,
        *,
        output_format: OutputFormat,
        stdout: TextIO,
        stderr: TextIO,
        stdout_is_tty: bool,
        stderr_is_tty: bool,
        no_color: bool,
        environ: Mapping[str, str],
        width: int = 80,
    ) -> None:
        self.output_format = output_format
        self._stdout = stdout
        self._stderr = stderr
        self._stdout_color = color_enabled(
            is_tty=stdout_is_tty,
            no_color=no_color,
            environ=environ,
        )
        self._stderr_color = color_enabled(
            is_tty=stderr_is_tty,
            no_color=no_color,
            environ=environ,
        )
        self._width = width

    def _console(self, *, error: bool) -> Console:
        color = self._stderr_color if error else self._stdout_color
        return Console(
            file=self._stderr if error else self._stdout,
            width=self._width,
            force_terminal=color,
            no_color=not color,
            color_system="standard" if color else None,
            highlight=False,
            soft_wrap=False,
        )

    def _write_json(self, value: Mapping[str, object], *, error: bool) -> None:
        stream = self._stderr if error else self._stdout
        serialized = _json_value(value)
        stream.write(
            json.dumps(serialized, ensure_ascii=False, separators=(",", ":")) + "\n"
        )

    def write_success(
        self,
        *,
        schema: str,
        data: Mapping[str, object] | BaseModel,
        table: TableData | None = None,
    ) -> None:
        """Write one schema-bearing JSON object or its explicit human table."""
        if _COMMAND_SCHEMA.fullmatch(schema) is None:
            raise OutputUsageError("Invalid command schema.")
        payload = _success_data(data)
        if self.output_format is OutputFormat.JSON:
            self._write_json({"schema": schema, **payload}, error=False)
            return
        if table is None:
            raise OutputUsageError("Table output requires human table data.")
        rich_table = Table(
            box=None,
            padding=(0, 1),
            pad_edge=False,
            header_style="bold cyan",
        )
        for column in table.columns:
            rich_table.add_column(
                column,
                justify="right" if column in table.right_align else "left",
            )
        for row in table.rows:
            rich_table.add_row(*(str(cell) for cell in row))
        self._console(error=False).print(rich_table)

    def write_error(self, error: ApiError | OutputError) -> None:
        """Write a stable machine envelope or a detail-free human error."""
        payload = {
            "schema": "solwyn.error.v1",
            "code": error.code,
            "message": error.message,
            "hint": error.hint,
            "detail": error.detail,
        }
        if self.output_format is OutputFormat.JSON:
            self._write_json(payload, error=True)
            return

        message = Text("Error ", style="bold red")
        message.append(f"[{error.code}]: ", style="bold")
        message.append(error.message)
        console = self._console(error=True)
        console.print(message)
        if error.hint is not None:
            hint = Text("Hint: ", style="bold yellow")
            hint.append(error.hint)
            console.print(hint)
