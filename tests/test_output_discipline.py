"""AST guard for the command-layer stdout/stderr contract."""

import ast
from pathlib import Path

import pytest


COMMANDS_DIR = Path("src/solwyn_cli/commands")
_LOCAL_DATA_EMISSIONS = {
    ("config.py", "get_config_value"),
    ("config.py", "list_config_values"),
}


class _OutputVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.functions: list[str] = []
        self.violations: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _record(self, node: ast.AST, message: str) -> None:
        self.violations.append(f"{self.path.name}:{node.lineno}: {message}")

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Name) and function.id == "print":
            self._record(node, "built-in print is forbidden in commands")
        elif isinstance(function, ast.Attribute) and function.attr == "print":
            self._record(node, "console.print is forbidden in commands")
        elif (
            isinstance(function, ast.Attribute) and function.attr in {"echo", "secho"}
        ) or (isinstance(function, ast.Name) and function.id in {"echo", "secho"}):
            current = self.functions[-1] if self.functions else "<module>"
            is_local_data = (self.path.name, current) in _LOCAL_DATA_EMISSIONS
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
            and isinstance(function.value, ast.Attribute)
            and function.value.attr == "stdout"
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
    local_command = tmp_path / "config.py"
    local_command.write_text(
        'def get_config_value():\n    typer.echo("value")\n'
        'def list_config_values():\n    typer.echo("key=value")\n',
        encoding="utf-8",
    )

    assert find_output_violations(stderr_command) == []
    assert find_output_violations(local_command) == []
