"""AST guard for the command-layer stdout/stderr contract."""

import ast
from pathlib import Path

import pytest


COMMANDS_DIR = Path("src/solwyn_cli/commands")
_LOCAL_DATA_CALLS = {
    ("keys.py", "env_key", "typer.echo(line)"),
    (
        "agent_context.py",
        "agent_context",
        "typer.echo(render_surface(ArtifactSurface.AGENT_CONTEXT), nl=False)",
    ),
    (
        "config.py",
        "get_config_value",
        "typer.echo('' if value is None else value)",
    ),
    (
        "config.py",
        "list_config_values",
        "typer.echo(f\"{key}={('' if value is None else value)}\")",
    ),
}


class _OutputVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.functions: list[str] = []
        self.violations: list[str] = []
        self.sys_aliases = {"sys"}
        self.stdout_aliases = {"stdout"}
        self.echo_aliases = {"echo", "secho"}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            if alias.name == "sys":
                self.sys_aliases.add(bound_name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if node.module == "sys" and alias.name == "stdout":
                self.stdout_aliases.add(bound_name)
            if node.module in {"click", "typer"} and alias.name in {"echo", "secho"}:
                self.echo_aliases.add(bound_name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _record(self, node: ast.AST, message: str) -> None:
        self.violations.append(f"{self.path.name}:{node.lineno}: {message}")

    def _is_stdout(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.stdout_aliases
        if not isinstance(node, ast.Attribute):
            return False
        if node.attr == "stdout":
            return (
                isinstance(node.value, ast.Name) and node.value.id in self.sys_aliases
            )
        if node.attr == "buffer":
            return self._is_stdout(node.value)
        return False

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Name) and function.id == "print":
            self._record(node, "built-in print is forbidden in commands")
        elif isinstance(function, ast.Attribute) and function.attr == "print":
            self._record(node, "console.print is forbidden in commands")
        elif (
            isinstance(function, ast.Attribute) and function.attr in {"echo", "secho"}
        ) or (isinstance(function, ast.Name) and function.id in self.echo_aliases):
            current = self.functions[-1] if self.functions else "<module>"
            call_fingerprint = (
                self.path.name,
                current,
                ast.unparse(node),
            )
            is_local_data = call_fingerprint in _LOCAL_DATA_CALLS
            is_stderr = any(
                keyword.arg == "err"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            if not is_local_data and not is_stderr:
                self._record(
                    node,
                    f"{ast.unparse(function)} must be statically directed to stderr",
                )
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "write"
            and self._is_stdout(function.value)
        ):
            self._record(node, "direct stdout.write is forbidden in commands")
        self.generic_visit(node)


def find_output_violations(path: Path) -> list[str]:
    """Return deterministic command output violations found through the AST."""
    visitor = _OutputVisitor(path)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.violations


def test_all_command_modules_obey_output_stream_discipline() -> None:
    violations = [
        violation
        for path in sorted(COMMANDS_DIR.rglob("*.py"))
        for violation in find_output_violations(path)
    ]

    assert violations == []


def test_interpolated_rich_status_text_trips_ast_guard(tmp_path: Path) -> None:
    command = tmp_path / "mutated.py"
    command.write_text(
        'def save(value):\n    typer.echo(f"[green]Saved {value}")\n',
        encoding="utf-8",
    )

    violations = find_output_violations(command)

    assert violations == [
        "mutated.py:2: typer.echo must be statically directed to stderr"
    ]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ('print("Saved")', "built-in print is forbidden"),
        ('console.print("Saved")', "console.print is forbidden"),
        ('sys.stdout.write("Saved")', "direct stdout.write is forbidden"),
        ('echo("Saved")', "echo must be statically directed to stderr"),
    ],
)
def test_direct_command_output_apis_trip_ast_guard(
    tmp_path: Path, statement: str, message: str
) -> None:
    command = tmp_path / "mutated.py"
    command.write_text(f"def save():\n    {statement}\n", encoding="utf-8")

    assert message in find_output_violations(command)[0]


def test_statically_proven_stderr_and_identified_local_data_are_allowed(
    tmp_path: Path,
) -> None:
    stderr_command = tmp_path / "remote.py"
    stderr_command.write_text(
        'def save():\n    typer.echo("Saved", err=True)\n', encoding="utf-8"
    )
    assert find_output_violations(stderr_command) == []
    assert find_output_violations(COMMANDS_DIR / "config.py") == []


def test_new_status_inside_local_data_function_trips_guard(tmp_path: Path) -> None:
    command = tmp_path / "config.py"
    source = (COMMANDS_DIR / "config.py").read_text(encoding="utf-8")
    command.write_text(
        source + '    typer.echo("Loaded configuration")\n', encoding="utf-8"
    )

    assert find_output_violations(command)[-1].endswith(
        "typer.echo must be statically directed to stderr"
    )


@pytest.mark.parametrize(
    "source",
    [
        'from sys import stdout as output\ndef save():\n    output.write("Saved")\n',
        'import sys as system\ndef save():\n    system.stdout.buffer.write(b"Saved")\n',
        'from typer import echo as emit\ndef save():\n    emit("Saved")\n',
        'from click import secho as emit\ndef save():\n    emit("Saved")\n',
    ],
)
def test_common_output_aliases_trip_guard(tmp_path: Path, source: str) -> None:
    command = tmp_path / "mutated.py"
    command.write_text(source, encoding="utf-8")

    assert find_output_violations(command) != []
