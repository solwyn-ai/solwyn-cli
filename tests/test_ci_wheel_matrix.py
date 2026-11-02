"""Exercise wheel platform selection: Linux on PRs and main pushes, every OS
on the weekly schedule and manual runs."""

import json
import os
from pathlib import Path
import subprocess

import pytest


SELECTOR = Path(__file__).resolve().parents[1] / ".github/scripts/wheel-matrix.sh"
LINUX = ["ubuntu-latest"]
ALL_OS = ["ubuntu-latest", "macos-latest", "windows-latest"]


def select(tmp_path: Path, event: str) -> list[str]:
    output = tmp_path / "selection-output"
    result = subprocess.run(
        ["bash", str(SELECTOR)],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_EVENT_NAME": event, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text().removeprefix("os="))


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_pull_requests_and_main_pushes_select_linux_only(tmp_path, event):
    assert select(tmp_path, event) == LINUX


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_scheduled_and_manual_runs_select_all_platforms(tmp_path, event):
    assert select(tmp_path, event) == ALL_OS


def test_publish_still_smoke_tests_macos_and_windows_before_release():
    publish = (SELECTOR.parents[1] / "workflows/publish.yml").read_text(
        encoding="utf-8"
    )
    assert 'tags: ["v*"]' in publish
    assert "os: [macos-latest, windows-latest]" in publish
    assert "needs: [build, test, packaging-smoke]" in publish
