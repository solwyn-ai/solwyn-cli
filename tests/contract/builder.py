"""Build the deterministic machine-readable Solwyn CLI contract.

The contract is derived, never hand-transcribed:

* The command tree and every flag/option (name, type, default, constraints)
  are introspected from the live Typer/Click app via ``typer.main.get_command``.
  There is no parallel hand-maintained list of commands or flags.
* Exit codes come from the frozen :class:`~solwyn_cli.exit_codes.ExitCode`
  enum; only the human *meaning* of each code is hand-authored, in
  ``EXIT_CODE_MEANINGS`` (kept honest by ``test_contract_snapshot.py``).
* JSON output schemas are captured from the checked-in golden fixtures in
  ``tests/golden``. Those fixtures are byte-for-byte asserted against real
  command output by the existing command tests, so the shapes here are the
  CLI's actual emitted JSON, not a description of it.

Only surface the CLI already exposes to users is enumerated. Endpoint URLs and
other HTTP internals are deliberately excluded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click
from typer.main import get_command

from solwyn_cli.exit_codes import ExitCode
from solwyn_cli.main import app

# The contract-format version. Bump only on a breaking change to the contract
# JSON *structure* (not on CLI changes — those change the payload, not this).
CONTRACT_VERSION = 1

CONTRACT_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = CONTRACT_DIR.parent / "golden"
SNAPSHOT_PATH = CONTRACT_DIR / "cli_contract.json"

# The universal machine error envelope every command emits on failure.
ERROR_SCHEMA = "solwyn.error.v1"

# Hand-authored meanings for the frozen exit-code contract. Verified against
# ``exit_codes.exit_code_for_error`` and the per-command help epilogs; the
# snapshot test asserts this registry covers ExitCode exactly.
EXIT_CODE_MEANINGS: dict[ExitCode, str] = {
    ExitCode.SUCCESS: "The command completed successfully.",
    ExitCode.GENERIC_ERROR: (
        "Unclassified failure: an API or network error, an invalid server "
        "response, or another runtime error."
    ),
    ExitCode.USAGE_ERROR: (
        "Invalid command-line usage: a missing, unknown, malformed, or "
        "conflicting option or argument."
    ),
    ExitCode.AUTH_ERROR: (
        "Authentication, authorization, or credential-storage failure "
        "(includes HTTP 401 and 403)."
    ),
    ExitCode.BUDGET_DENIED: (
        "A budget guardrail denied spend or a utilization gate tripped."
    ),
    ExitCode.NOT_FOUND: (
        "The requested resource does not exist (includes HTTP 404), such as a "
        "missing project or an expired or already-consumed reservation."
    ),
}

_SCHEMA_TOKEN = re.compile(r"solwyn\.[a-z0-9._-]+\.v1")
_JSON_SCHEMA_LINE = re.compile(r"JSON schema:\s*(.+)")


def _jsonify_default(value: object) -> Any:
    """Reduce a Click default to a stable, JSON-serialisable value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, (str, int, float, bool)):
        return inner
    return str(value)


def _param_entry(param: click.Parameter) -> dict[str, Any]:
    """Describe one option or argument from the live Click parameter."""
    param_type = param.type
    entry: dict[str, Any] = {
        "name": param.name,
        "kind": "argument" if isinstance(param, click.Argument) else "option",
        "cli_names": [*param.opts, *(param.secondary_opts or [])],
        "type": param_type.name,
        "required": bool(param.required),
        "default": _jsonify_default(param.default),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "multiple": bool(param.multiple),
        "hidden": bool(getattr(param, "hidden", False)),
        "help": getattr(param, "help", None) or None,
    }
    choices = getattr(param_type, "choices", None)
    if choices is not None:
        entry["choices"] = list(choices)
    if hasattr(param_type, "min") or hasattr(param_type, "max"):
        entry["min"] = getattr(param_type, "min", None)
        entry["max"] = getattr(param_type, "max", None)
    return entry


def _declared_schema_line(command: click.Command) -> str | None:
    match = _JSON_SCHEMA_LINE.search(command.epilog or "")
    return match.group(1).strip() if match else None


def _schema_token(schema_line: str | None) -> str | None:
    if not schema_line:
        return None
    match = _SCHEMA_TOKEN.search(schema_line)
    return match.group(0) if match else None


def _output_kind(schema_line: str | None, schema_token: str | None) -> str | None:
    """Classify how a command emits output, from its declared help contract."""
    if schema_token is not None:
        return "json-envelope"
    if schema_line is None:
        return None
    lowered = schema_line.lower()
    if lowered.startswith("stream"):
        return "stream"
    if "mcp" in lowered:
        return "mcp-stdio"
    if lowered.startswith("varies"):
        return "varies-by-subcommand"
    if lowered.startswith("none"):
        return "plain-text"
    return None


def _walk(
    command: click.Command, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], click.Command]]:
    nodes = [(path, command)]
    for name, child in sorted(getattr(command, "commands", {}).items()):
        nodes.extend(_walk(child, (*path, name)))
    return nodes


def _command_entries() -> list[dict[str, Any]]:
    root = get_command(app)
    entries: list[dict[str, Any]] = []
    for path, command in _walk(root):
        schema_line = _declared_schema_line(command)
        schema_token = _schema_token(schema_line)
        entries.append(
            {
                "path": list(path),
                "name": path[-1] if path else "solwyn",
                "is_group": hasattr(command, "commands"),
                "hidden": bool(getattr(command, "hidden", False)),
                "summary": (command.help or "").strip() or None,
                "json_schema": schema_token,
                "output_kind": _output_kind(schema_line, schema_token),
                "params": [_param_entry(p) for p in command.params],
            }
        )
    return entries


def declared_schema_tokens() -> set[str]:
    """Every ``solwyn.*.v1`` schema the live CLI declares it emits."""
    tokens: set[str] = set()
    for _, command in _walk(get_command(app)):
        token = _schema_token(_declared_schema_line(command))
        if token is not None:
            tokens.add(token)
    return tokens


def golden_schema_keys() -> dict[str, list[set[str]]]:
    """Top-level key sets of every checked-in golden JSON fixture, per schema."""
    by_schema: dict[str, list[set[str]]] = {}
    for golden in sorted(GOLDEN_DIR.glob("*.json")):
        payload = json.loads(golden.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        schema = payload.get("schema")
        if not isinstance(schema, str):
            continue
        by_schema.setdefault(schema, []).append(set(payload))
    return by_schema


def _exit_code_entries() -> list[dict[str, Any]]:
    return [
        {
            "code": code.value,
            "name": code.name,
            "meaning": EXIT_CODE_MEANINGS[code],
        }
        for code in sorted(ExitCode, key=lambda member: member.value)
    ]


def _json_schema_entries() -> list[dict[str, Any]]:
    """Captured JSON output shapes, restricted to schemas the CLI truly emits."""
    valid = declared_schema_tokens() | {ERROR_SCHEMA}
    by_schema = golden_schema_keys()
    entries: list[dict[str, Any]] = []
    for schema in sorted(by_schema):
        if schema not in valid:
            continue
        key_sets = by_schema[schema]
        all_keys = set().union(*key_sets)
        required = set.intersection(*key_sets)
        entries.append(
            {
                "schema": schema,
                "keys": sorted(all_keys),
                "optional_keys": sorted(all_keys - required),
                "universal_error_envelope": schema == ERROR_SCHEMA,
            }
        )
    return entries


def build_cli_contract() -> dict[str, Any]:
    """Assemble the full deterministic CLI contract document."""
    return {
        "contract_version": CONTRACT_VERSION,
        "commands": _command_entries(),
        "exit_codes": _exit_code_entries(),
        "json_schemas": _json_schema_entries(),
    }


def render_contract() -> str:
    """Serialise the contract deterministically (stable keys, trailing newline)."""
    return (
        json.dumps(
            build_cli_contract(),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n"
    )
