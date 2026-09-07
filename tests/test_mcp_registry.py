"""Offline schema validity is separate from release/namespace ownership."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "docs/registry/server.draft.json"


def validate(path):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_mcp_registry.py"),
            "--draft",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_registry_draft_validates_offline():
    result = validate(DRAFT)
    assert result.returncode == 0, result.stderr
    assert "Unpublished draft shape and metadata verified" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "mutation",
    ["version", "namespace", "secret", "transport", "profile", "order", "description"],
)
def test_invalid_or_misleading_draft_fails(tmp_path, mutation):
    assert DRAFT.is_file(), "Registry draft is missing"
    draft = json.loads(DRAFT.read_text())
    package = draft["packages"][0]
    if mutation == "version":
        package["version"] = "9.9.9"
    elif mutation == "namespace":
        draft["name"] = "io.github.someone/solwyn"
    elif mutation == "secret":
        package["environmentVariables"][0]["default"] = "PRIVATE_KEY_CANARY"
    elif mutation == "transport":
        package["transport"] = {
            "type": "streamable-http",
            "url": "https://example.test",
        }
    elif mutation == "profile":
        package["packageArguments"][-1]["value"] = "guardrails"
    elif mutation == "order":
        package["packageArguments"].reverse()
    else:
        draft["description"] = "x" * 101
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(draft))
    result = validate(path)
    assert result.returncode == 1
    assert "PRIVATE_KEY_CANARY" not in result.stderr
