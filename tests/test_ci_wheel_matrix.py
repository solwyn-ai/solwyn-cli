"""Exercise wheel platform selection against real PR histories."""

import json
import os
from pathlib import Path
import subprocess

import pytest


SELECTOR = Path(__file__).resolve().parents[1] / ".github/scripts/wheel-matrix.sh"
LINUX = ["ubuntu-latest"]
ALL_OS = ["ubuntu-latest", "macos-latest", "windows-latest"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def history(tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "ci-test@example.invalid")
    git(tmp_path, "config", "user.name", "CI test")
    (tmp_path / "pyproject.toml").write_text("# packaging fixture\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def select(repo, base, *, event="pull_request", head="HEAD"):
    output = repo / "selection-output"
    result = subprocess.run(
        ["bash", str(SELECTOR)],
        cwd=repo,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_OUTPUT": str(output),
            "PR_BASE_SHA": base,
            "PR_HEAD_SHA": head,
        },
        capture_output=True,
        text=True,
    )
    return result, output


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/guide.md", LINUX),
        ("server.json", LINUX),
        ("README.md", LINUX),
        ("LICENSE", LINUX),
        (".github/ISSUE_TEMPLATE/bug_report.yml", LINUX),
        ("scripts/budget-plane-cli-smoke.py", LINUX),
        ("scripts/budget-plane-mcp-smoke.py", LINUX),
        ("scripts/budget-plane-smoke.sh", LINUX),
        ("scripts/validate_mcp_registry.py", LINUX),
        ("src/solwyn_cli/commands/costs.py", LINUX),
        ("tests/test_costs_command.py", LINUX),
        ("tests/test_x.py", LINUX),
        ("tests/test_ci_wheel_matrix.py", LINUX),
        ("tests/test_windows_paths.py", LINUX),
        ("src/solwyn_cli/_vendor/wire.py", LINUX),
        ("pyproject.toml", ALL_OS),
        ("uv.lock", ALL_OS),
        ("hatch_build.py", ALL_OS),
        ("build.py", ALL_OS),
        ("setup.cfg", ALL_OS),
        ("MANIFEST.in", ALL_OS),
        ("requirements-dev.txt", ALL_OS),
        ("constraints-test.txt", ALL_OS),
        (".python-version", ALL_OS),
        (".gitattributes", ALL_OS),
        (".github/workflows/ci.yml", ALL_OS),
        (".github/workflows/publish.yml", ALL_OS),
        (".github/scripts/wheel-matrix.sh", ALL_OS),
        ("src/solwyn_cli/mcp/server.py", ALL_OS),
        ("src/solwyn_cli/_mcp_doctor_pipe.py", ALL_OS),
        ("src/solwyn_cli/commands/mcp_config.py", ALL_OS),
        ("src/solwyn_cli/commands/init.py", ALL_OS),
        ("src/solwyn_cli/commands/projects.py", ALL_OS),
        ("src/solwyn_cli/commands/plan.py", ALL_OS),
        ("src/solwyn_cli/commands/invite.py", ALL_OS),
        ("src/solwyn_cli/commands/agent_context.py", ALL_OS),
        ("src/solwyn_cli/config.py", ALL_OS),
        ("src/solwyn_cli/auth.py", ALL_OS),
        ("src/solwyn_cli/main.py", ALL_OS),
        ("src/solwyn_cli/artifacts/_atomic.py", ALL_OS),
        ("src/solwyn_cli/new_shared_module.py", ALL_OS),
        ("tests/test_mcp_wheel_runner.py", LINUX),
        ("tests/fixtures/mcp_doctor_wheel_smoke.py", ALL_OS),
        ("tests/fixtures/new_client.py", ALL_OS),
        ("tests/test_atomic_rename.py", LINUX),
        ("tests/test_projects_command.py", LINUX),
        ("tests/test_plan_command.py", LINUX),
        ("tests/test_billing_lifecycle.py", LINUX),
        ("tests/test_billing_errors_preview.py", LINUX),
        ("tests/test_team_account.py", LINUX),
        ("tests/test_reliability_round2.py", LINUX),
        ("tests/conftest.py", LINUX),
        ("examples/mcp/typescript/package-lock.json", ALL_OS),
        ("scripts/locks/mcp-legacy.txt", ALL_OS),
        ("scripts/locks/mcp-legacy.in", ALL_OS),
        ("scripts/mcp_wheel_smoke.py", ALL_OS),
        ("scripts/inspect_wheel.py", ALL_OS),
        ("src/solwyn_cli/commands/windows_helper.py", ALL_OS),
        ("src/solwyn_cli/commands/path with spaces.sh", ALL_OS),
    ],
)
def test_pr_platforms_follow_packaging_and_platform_changes(history, path, expected):
    repo, base = history
    changed = repo / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("# changed\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "PR change")

    result, output = select(repo, base)

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text().removeprefix("os=")) == expected


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_non_pr_events_always_select_all_platforms(history, event):
    repo, _ = history
    result, output = select(repo, "", event=event, head="")
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text().removeprefix("os=")) == ALL_OS


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_removing_a_sensitive_path_still_requires_all_platforms(history, operation):
    repo, base = history
    if operation == "delete":
        git(repo, "rm", "pyproject.toml")
    else:
        git(repo, "mv", "pyproject.toml", "guide.md")
    git(repo, "commit", "-m", operation)

    result, output = select(repo, base)

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text().removeprefix("os=")) == ALL_OS


def test_base_only_changes_do_not_expand_a_docs_pr(history):
    repo, base = history
    git(repo, "checkout", "-b", "feature")
    (repo / "guide.md").write_text("Docs only\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "docs PR")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    (repo / "pyproject.toml").write_text("# base-only packaging change\n")
    git(repo, "commit", "-am", "base moved")
    base = git(repo, "rev-parse", "HEAD")

    result, output = select(repo, base, head=head)

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text().removeprefix("os=")) == LINUX


def test_invalid_diff_fails_without_emitting_a_linux_only_matrix(history):
    repo, _ = history
    result, output = select(repo, "missing-base")
    assert result.returncode != 0
    assert not output.exists()
