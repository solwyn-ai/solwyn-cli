"""What the CLI tells agents and people never leaks a key or invites a switch.

1. Every surface that says how to hand a key to an SDK (init --agents files,
   agent-context, MCP tool text, keys env help) gives the subshell form with an
   explicit table format; none gives a bare `eval "$(solwyn keys env)"`, which
   leaves the key in a long-lived shell and, under a JSON format, let zsh echo
   it (0.5.0 run-through finding 1).
2. No hint tells the reader to edit .solwyn.env or point SOLWYN_PROJECT at
   another project when a project is already chosen; the guidance says never
   to change either to get past a limit (finding 2).
"""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solwyn_cli.artifacts import ArtifactSurface, agent_guidance, render_surface
from solwyn_cli.commands import keys as keys_module
from solwyn_cli.main import app
from solwyn_cli.mcp import server
from test_keys_env_shell import KEYS_ENV_SUBSHELL

SRC = Path(__file__).resolve().parents[1] / "src" / "solwyn_cli"
# `eval "$(solwyn ...` without --format table right after `solwyn`.
UNSAFE_EVAL = re.compile(r'eval\s+"\$\(solwyn(?!\s+--format\s+table\s)')


def _surfaces() -> dict[str, str]:
    texts = {
        f"artifact:{surface.value}": render_surface(surface)
        for surface in ArtifactSurface
    }
    texts["agent_guidance"] = agent_guidance()
    texts["mcp_tools"] = json.dumps(
        [tool.definition.model_dump(mode="json") for tool in server.get_tool_catalog()]
    )
    help_result = CliRunner().invoke(app, ["keys", "env", "--help"], terminal_width=400)
    assert help_result.exit_code == 0
    texts["keys env --help"] = help_result.stdout
    return texts


def test_the_shell_tests_exercise_the_form_the_cli_gives() -> None:
    assert getattr(keys_module, "KEYS_ENV_SUBSHELL", None) == KEYS_ENV_SUBSHELL


@pytest.mark.parametrize("name", sorted(_surfaces()))
def test_no_surface_gives_a_bare_keys_env_eval(name: str) -> None:
    text = _surfaces()[name]
    assert not UNSAFE_EVAL.search(text), name
    assert 'eval "$(solwyn keys env)"' not in text


@pytest.mark.parametrize(
    "surface",
    [
        *(f"artifact:{s.value}" for s in ArtifactSurface),
        "agent_guidance",
        "keys env --help",
    ],
)
def test_key_handoff_surfaces_give_the_subshell_form(surface: str) -> None:
    assert KEYS_ENV_SUBSHELL in " ".join(_surfaces()[surface].split())


def test_no_source_string_gives_a_bare_keys_env_eval() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if UNSAFE_EVAL.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("surface", [s.value for s in ArtifactSurface])
def test_guidance_says_never_to_change_the_project_to_get_past_a_limit(
    surface: str,
) -> None:
    text = " ".join(render_surface(ArtifactSurface(surface)).split())
    assert "Do not change it to reach another project's budget" in text
    assert (
        "Never point SOLWYN_PROJECT or `--project` at another project to get past "
        "a budget limit or a denial" in text
    )


def test_the_mcp_mismatch_hint_asks_a_person() -> None:
    _, hint = server._LOCAL_PROJECT_ERRORS["project_mismatch"]
    assert hint is not None
    assert hint.startswith("Ask a person to change .solwyn.env")
    assert "Never switch projects to get past a budget limit." in hint


def test_no_hint_tells_the_reader_to_edit_the_repo_file() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\bEdit\b.{0,20}(\.solwyn\.env|REPO_FILE_NAME)", line)
    ]
    assert offenders == []
