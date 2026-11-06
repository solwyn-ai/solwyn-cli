"""keys env in real shells: no format setting can make a shell echo the key.

The 0.5.0 run-through found that with SOLWYN_FORMAT=json, `keys env` printed
one JSON word holding the export lines, and zsh's eval failed on it with
"no matches found: <the whole word>", key included. keys env now prints JSON
only when --json or --format json is on its own command line; SOLWYN_FORMAT and
config format never apply to it. These tests run the installed CLI under bash
and zsh (each skipped when absent) and assert the key never reaches the
shell's stdout or stderr.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from key_fixtures import KEY, OTHER_KEY, OTHER_PROJECT_ID, store_key

# The form agent guidance, help and docs give (test_agent_guidance_safety
# checks every surface uses it).
KEYS_ENV_SUBSHELL = (
    '(solwyn_env="$(solwyn --format table keys env)" && eval "$solwyn_env" '
    "&& exec <command>)"
)

SHELLS = [
    pytest.param(
        name,
        marks=pytest.mark.skipif(shutil.which(name) is None, reason=f"no {name}"),
    )
    for name in ("bash", "zsh")
]


@pytest.fixture
def shell_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A repo with a stored key for its project, and the CLI on PATH."""
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    store_key(project_name="checkout-bot")
    # research's key too, so a SOLWYN_PROJECT=research run reaches the
    # mismatch check (and must never print either key).
    store_key(project_id=OTHER_PROJECT_ID, key=OTHER_KEY, project_name="research")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".solwyn.env").write_text('SOLWYN_PROJECT="checkout-bot"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "solwyn"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -c '
        '"from solwyn_cli.main import app; app()" "$@"\n'
    )
    launcher.chmod(0o755)
    zdotdir = tmp_path / "zdotdir"
    zdotdir.mkdir()
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SOLWYN_") and key not in ("BASH_ENV", "ENV")
    }
    environ.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "SOLWYN_CREDENTIAL_STORE": "file",
            "SOLWYN_CREDENTIALS_FILE": os.environ["SOLWYN_CREDENTIALS_FILE"],
            "ZDOTDIR": str(zdotdir),
            "PWD": str(repo),
        }
    )
    environ["_REPO"] = str(repo)
    return environ


def _run(shell: str, script: str, environ: dict[str, str], **extra: str):
    return subprocess.run(
        [shell, "-c", script],
        cwd=environ["_REPO"],
        env={**environ, **extra},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("shell", SHELLS)
def test_an_implicit_json_format_still_evals_and_never_echoes_the_key(
    shell: str, shell_env: dict[str, str]
) -> None:
    # The run-through's repro: SOLWYN_FORMAT=json zsh -c 'eval "$(solwyn keys env)"'
    result = _run(
        shell,
        'eval "$(solwyn keys env)" && printf "%s %s" "${#SOLWYN_API_KEY}" '
        '"$SOLWYN_PROJECT"',
        shell_env,
        SOLWYN_FORMAT="json",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{len(KEY)} checkout-bot"
    assert KEY not in result.stdout + result.stderr
    assert OTHER_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_an_implicit_json_config_format_still_evals(
    shell: str, shell_env: dict[str, str]
) -> None:
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "solwyn" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('format = "json"\n')

    result = _run(shell, 'eval "$(solwyn keys env)" && echo ok', shell_env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert KEY not in result.stdout + result.stderr
    assert OTHER_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_a_failing_keys_env_under_eval_prints_no_key(
    shell: str, shell_env: dict[str, str]
) -> None:
    # A project other than .solwyn.env's: keys env refuses on stderr and
    # prints nothing to stdout, so eval gets an empty string.
    result = _run(
        shell,
        'eval "$(solwyn keys env)"; echo "key:${SOLWYN_API_KEY:-none}"',
        shell_env,
        SOLWYN_FORMAT="json",
        SOLWYN_PROJECT="research",
    )

    assert "project_mismatch" in result.stderr
    assert result.stdout == "key:none\n"
    assert KEY not in result.stdout + result.stderr
    assert OTHER_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("project", ["checkout-bot", "research"])
def test_the_guidance_subshell_never_echoes_the_key(
    shell: str, shell_env: dict[str, str], project: str
) -> None:
    # The command fails (not found) when the key was handed over, and never
    # starts when keys env refused: neither prints the key.
    script = (
        KEYS_ENV_SUBSHELL.replace("<command>", "solwyn-no-such-command-for-test")
        + '; echo "parent:${SOLWYN_API_KEY:-none}"'
    )

    result = _run(
        shell, script, shell_env, SOLWYN_FORMAT="json", SOLWYN_PROJECT=project
    )

    assert result.stdout == "parent:none\n"
    assert KEY not in result.stdout + result.stderr
    assert OTHER_KEY not in result.stdout + result.stderr
    if project == "research":
        assert "project_mismatch" in result.stderr
        assert "solwyn-no-such-command-for-test" not in result.stderr
    else:
        assert "solwyn-no-such-command-for-test" in result.stderr
