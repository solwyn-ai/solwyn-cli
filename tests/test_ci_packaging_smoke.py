"""Release-readiness guards for CI and the built wheel artifact."""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


WORKFLOW_PATH = Path(".github/workflows/ci.yml")
INSPECTOR_PATH = Path("scripts/inspect_wheel.py")
ACTION_PIN = re.compile(r"^\s*- uses: [^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$")
RUNTIME_REQUIREMENTS = (
    "click>=8.1",
    "httpx>=0.27",
    "keyring>=25",
    "mcp==2.1.1",
    "platformdirs>=4",
    "pydantic>=2",
    "rich>=13",
    "tomli-w>=1",
    "typer>=0.12",
)
EXPECTED_PACKAGE_FILES = (
    "solwyn_cli/__init__.py",
    "solwyn_cli/artifacts/__init__.py",
    "solwyn_cli/help.py",
    "solwyn_cli/mcp/server.py",
)


def _job_block(workflow: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    assert marker in workflow, f"CI is missing the {job_name!r} job"
    remainder = workflow.split(marker, maxsplit=1)[1]
    next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n", remainder)
    return remainder if next_job is None else remainder[: next_job.start()]


def _metadata(requirements: tuple[str, ...] = RUNTIME_REQUIREMENTS) -> str:
    lines = [
        "Metadata-Version: 2.4",
        "Name: solwyn-cli",
        "Version: 0.1.0",
        "License-File: LICENSE",
        *(f"Requires-Dist: {requirement}" for requirement in requirements),
    ]
    return "\n".join(lines) + "\n"


def _write_wheel(
    path: Path,
    *,
    metadata: str | None = None,
    package_files: tuple[str, ...] = EXPECTED_PACKAGE_FILES,
    extra_files: tuple[str, ...] = (),
) -> None:
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("solwyn_cli-0.1.0.dist-info/METADATA", metadata or _metadata())
        for package_file in package_files:
            wheel.writestr(package_file, "")
        for extra_file in extra_files:
            wheel.writestr(extra_file, "")


def _inspect(wheel: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSPECTOR_PATH), str(wheel)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_packaging_smoke_job_tests_the_wheel_not_the_source_tree() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    job = _job_block(workflow, "packaging-smoke")

    assert "uv sync --locked --all-groups" in job
    assert "uv build --clear" in job
    assert "wheels=(dist/*.whl)" in job
    assert '"${#wheels[@]}" -ne 1' in job
    assert 'uvx --from "$wheel" solwyn --version' in job
    assert 'uvx --from "$wheel" solwyn-cli --version' in job
    assert re.search(r'mktemp -d.+cd "\$smoke_dir"', job, flags=re.DOTALL)
    assert not re.search(r"\buv run(?:\s+--\S+)*\s+solwyn(?:-cli)?\b", job)


def test_packaging_smoke_job_pins_actions_and_inspects_distribution() -> None:
    job = _job_block(WORKFLOW_PATH.read_text(encoding="utf-8"), "packaging-smoke")
    action_lines = [line for line in job.splitlines() if "uses:" in line]

    assert any("actions/checkout@" in line for line in action_lines)
    assert any("actions/setup-python@" in line for line in action_lines)
    assert any("astral-sh/setup-uv@" in line for line in action_lines)
    assert action_lines
    assert all(ACTION_PIN.match(line) for line in action_lines)
    assert "scripts/inspect_wheel.py" in job


def test_wheel_inspector_accepts_complete_distribution(tmp_path: Path) -> None:
    wheel = tmp_path / "solwyn_cli-0.1.0-py3-none-any.whl"
    _write_wheel(wheel)

    result = _inspect(wheel)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "Wheel metadata and contents verified" in result.stderr


@pytest.mark.parametrize(
    ("package_files", "extra_files", "message"),
    [
        (
            tuple(
                path
                for path in EXPECTED_PACKAGE_FILES
                if path != "solwyn_cli/mcp/server.py"
            ),
            (),
            "missing expected package file",
        ),
        (EXPECTED_PACKAGE_FILES, ("CLAUDE.md",), "forbidden development file"),
        (
            EXPECTED_PACKAGE_FILES,
            ("solwyn_cli/.claude/settings.json",),
            "forbidden development file",
        ),
    ],
)
def test_wheel_inspector_rejects_incomplete_or_private_contents(
    tmp_path: Path,
    package_files: tuple[str, ...],
    extra_files: tuple[str, ...],
    message: str,
) -> None:
    wheel = tmp_path / "solwyn_cli-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, package_files=package_files, extra_files=extra_files)

    result = _inspect(wheel)

    assert result.returncode == 1
    assert message in result.stderr


def test_wheel_inspector_rejects_path_dependency_leakage(tmp_path: Path) -> None:
    wheel = tmp_path / "solwyn_cli-0.1.0-py3-none-any.whl"
    requirements = tuple(
        "mcp @ file:///workspace/mcp"
        if requirement.startswith("mcp==")
        else requirement
        for requirement in RUNTIME_REQUIREMENTS
    )
    _write_wheel(wheel, metadata=_metadata(requirements))

    result = _inspect(wheel)

    assert result.returncode == 1
    assert "path or workspace dependency" in result.stderr
