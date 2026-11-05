"""Release-readiness guards for CI and the built wheel artifact."""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

WORKFLOW_PATH = Path(".github/workflows/ci.yml")
PUBLISH_WORKFLOW_PATH = Path(".github/workflows/publish.yml")
INSPECTOR_PATH = Path("scripts/inspect_wheel.py")
ACTION_PIN = re.compile(r"^\s*- uses: [^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$")
PACKAGE_VERSION = "0.4.3"
METADATA_2_5_INSPECTOR = (
    "hynek/build-and-inspect-python-package@"
    "2abe76da66d0a6a4a227101f9348ee855797cfa5 # v3.0.1"
)
METADATA_2_5_PUBLISHER = (
    "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # release/v1"
)
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
    "solwyn_cli/_mcp_launch.py",
    "solwyn_cli/_mcp_doctor_helper.py",
    "solwyn_cli/_mcp_doctor_ipc.py",
    "solwyn_cli/_mcp_doctor_pipe.py",
    "solwyn_cli/_mcp_doctor_process.py",
    "solwyn_cli/commands/mcp_config.py",
    "solwyn_cli/commands/mcp_doctor.py",
)


def _job_block(workflow: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    assert marker in workflow, f"CI is missing the {job_name!r} job"
    remainder = workflow.split(marker, maxsplit=1)[1]
    next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n", remainder)
    return remainder if next_job is None else remainder[: next_job.start()]


def _job_condition(job: str) -> str:
    condition = re.search(r"(?m)^    if: (.+(?:\n      .+)*)", job)
    assert condition is not None, "Job is missing its event/draft guard"
    return " ".join(condition[1].removeprefix(">-").split())


def _metadata(requirements: tuple[str, ...] = RUNTIME_REQUIREMENTS) -> str:
    lines = [
        "Metadata-Version: 2.5",
        "Name: solwyn-cli",
        f"Version: {PACKAGE_VERSION}",
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
        wheel.writestr(
            f"solwyn_cli-{PACKAGE_VERSION}.dist-info/METADATA",
            metadata or _metadata(),
        )
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
    assert "scripts/mcp_wheel_smoke.py --python" in job
    assert "os: ${{ fromJSON(needs.changes.outputs.wheel-os) }}" in job
    assert 'python-version: ["3.11"]' in job
    assert 'node-version: "24"' in job
    assert not re.search(r"\buv run(?:\s+--\S+)*\s+solwyn(?:-cli)?\b", job)


def test_protected_job_names_and_real_legacy_client_stay_maintained():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "name: Quality\n" in _job_block(workflow, "quality")
    tests = _job_block(workflow, "tests")
    assert "name: Tests (Python ${{ matrix.python-version }})" in tests
    for version in ("3.11", "3.12", "3.13"):
        assert f'- "{version}"' in tests
    assert "SOLWYN_TEST_LEGACY_PYTHON:" in tests
    assert "--require-hashes scripts/locks/mcp-legacy.txt" in tests
    for job_name in ("quality", "tests"):
        assert not re.search(r"(?m)^    needs:", _job_block(workflow, job_name))


def test_ci_concurrency_cancels_only_pr_runs_and_isolates_other_runs() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    concurrency = re.search(r"(?m)^concurrency:\n((?:  .*\n)+)", workflow)
    assert concurrency is not None
    assert (
        "  group: ${{ github.workflow }}-"
        "${{ github.event_name == 'pull_request' && github.ref || github.run_id }}\n"
    ) in concurrency[1]
    assert (
        "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n"
    ) in concurrency[1]


@pytest.mark.parametrize("job_name", ["changes", "quality", "packaging-smoke"])
def test_ci_jobs_skip_draft_prs(job_name: str) -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert _job_condition(_job_block(workflow, job_name)) == (
        "github.event_name != 'pull_request' || !github.event.pull_request.draft"
    )
    assert "types: [opened, synchronize, reopened, ready_for_review]" in workflow


def test_ci_python_tests_skip_only_pushes_and_draft_prs() -> None:
    job = _job_block(WORKFLOW_PATH.read_text(encoding="utf-8"), "tests")

    assert _job_condition(job) == (
        "github.event_name != 'push' && "
        "(github.event_name != 'pull_request' || !github.event.pull_request.draft)"
    )


def test_ci_selector_needs_no_pr_diff() -> None:
    job = _job_block(WORKFLOW_PATH.read_text(encoding="utf-8"), "changes")

    assert "run: bash .github/scripts/wheel-matrix.sh" in job
    assert "fetch-depth" not in job
    assert "PR_BASE_SHA" not in job


def test_packaging_smoke_job_pins_actions_and_inspects_distribution() -> None:
    job = _job_block(WORKFLOW_PATH.read_text(encoding="utf-8"), "packaging-smoke")
    action_lines = [line for line in job.splitlines() if "uses:" in line]

    assert any("actions/checkout@" in line for line in action_lines)
    assert any("actions/setup-python@" in line for line in action_lines)
    assert any("astral-sh/setup-uv@" in line for line in action_lines)
    assert action_lines
    assert all(ACTION_PIN.match(line) for line in action_lines)
    runner = Path("scripts/mcp_wheel_smoke.py").read_text()
    assert "scripts/inspect_wheel.py" in runner


def test_publish_workflow_uses_metadata_2_5_compatible_inspector() -> None:
    workflow = PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert METADATA_2_5_INSPECTOR in workflow


def test_publish_workflow_uses_metadata_2_5_compatible_publisher() -> None:
    workflow = PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert METADATA_2_5_PUBLISHER in workflow


def test_publish_workflow_rejects_a_tag_that_differs_from_package_version() -> None:
    workflow = PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "PACKAGE_VERSION: ${{ steps.package.outputs.package_version }}" in workflow
    assert 'if [[ "v${PACKAGE_VERSION}" != "${GITHUB_REF_NAME}" ]]; then' in workflow


def test_publish_workflow_tests_wheel_with_all_locked_test_dependencies() -> None:
    workflow = PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")
    job = _job_block(workflow, "test")

    assert "uv sync --locked --all-groups --no-install-project" in job
    assert "uv pip install --python .venv/bin/python --no-deps dist/*.whl" in job


def test_publish_requires_cross_platform_wheel_acceptance() -> None:
    workflow = PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")
    publish = _job_block(workflow, "publish")
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", publish)
    assert needs is not None
    assert {"build", "test", "packaging-smoke"} <= {
        job.strip() for job in needs[1].split(",")
    }

    smoke = _job_block(workflow, "packaging-smoke")
    assert "    needs: build\n" in smoke
    assert "        os: [macos-latest, windows-latest]\n" in smoke
    assert '        python-version: ["3.11"]\n' in smoke
    assert "    defaults:\n      run:\n        shell: bash\n" in smoke
    assert all(
        shell == "bash" for shell in re.findall(r"(?m)^        shell: (.+)$", smoke)
    )
    assert "          name: Packages\n          path: dist\n" in smoke
    assert (
        "        run: uv run --no-sync python scripts/mcp_wheel_smoke.py "
        '--python "${{ matrix.python-version }}" --wheel dist/*.whl\n'
    ) in smoke


@pytest.mark.parametrize("action", ["actions/checkout", "actions/setup-python"])
def test_release_wheel_gate_uses_the_existing_publish_action_pins(action: str) -> None:
    workflow = PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8")
    pin = rf"(?m)^      - uses: {re.escape(action)}@([0-9a-f]{{40}})\b"
    existing = re.search(pin, _job_block(workflow, "test"))
    smoke = re.search(pin, _job_block(workflow, "packaging-smoke"))

    assert existing is not None and smoke is not None
    assert smoke[1] == existing[1]


def test_wheel_inspector_accepts_complete_distribution(tmp_path: Path) -> None:
    wheel = tmp_path / f"solwyn_cli-{PACKAGE_VERSION}-py3-none-any.whl"
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
    wheel = tmp_path / f"solwyn_cli-{PACKAGE_VERSION}-py3-none-any.whl"
    _write_wheel(wheel, package_files=package_files, extra_files=extra_files)

    result = _inspect(wheel)

    assert result.returncode == 1
    assert message in result.stderr


def test_wheel_inspector_rejects_path_dependency_leakage(tmp_path: Path) -> None:
    wheel = tmp_path / f"solwyn_cli-{PACKAGE_VERSION}-py3-none-any.whl"
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


def test_wheel_inspector_requires_the_isolated_doctor_helper(tmp_path):
    wheel = tmp_path / f"solwyn_cli-{PACKAGE_VERSION}-py3-none-any.whl"
    _write_wheel(
        wheel,
        package_files=tuple(
            path
            for path in EXPECTED_PACKAGE_FILES
            if path != "solwyn_cli/_mcp_doctor_helper.py"
        ),
    )
    result = _inspect(wheel)
    assert result.returncode == 1
    assert "missing expected package file" in result.stderr
