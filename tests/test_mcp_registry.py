"""Offline server.json validity is separate from namespace ownership."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.json"


def validate(path):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_mcp_registry.py"),
            "--server",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_server_json_validates_offline():
    result = validate(SERVER)
    assert result.returncode == 0, result.stderr
    assert "server.json shape, README marker and release metadata verified" in (
        result.stderr
    )
    assert result.stdout == ""


def test_server_json_matches_the_package_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    server = json.loads(SERVER.read_text())
    (package,) = server["packages"]

    assert server["version"] == project["version"]
    assert package["version"] == project["version"]
    assert package["identifier"] == project["name"]
    assert project["readme"] == "README.md"
    assert f"<!-- mcp-name: {server['name']} -->" in (ROOT / "README.md").read_text()


@pytest.mark.parametrize(
    "mutation",
    ["version", "namespace", "secret", "transport", "profile", "order", "description"],
)
def test_invalid_or_misleading_server_json_fails(tmp_path, mutation):
    assert SERVER.is_file(), "server.json is missing"
    server = json.loads(SERVER.read_text())
    package = server["packages"][0]
    if mutation == "version":
        package["version"] = "9.9.9"
    elif mutation == "namespace":
        server["name"] = "io.github.someone/solwyn"
    elif mutation == "secret":
        package["environmentVariables"][0]["default"] = "PRIVATE_KEY_CANARY"
    elif mutation == "transport":
        package["transport"] = {
            "type": "streamable-http",
            "url": "https://example.test",
        }
    elif mutation == "profile":
        package["packageArguments"][-1]["value"] = "read"
    elif mutation == "order":
        package["packageArguments"].reverse()
    else:
        server["description"] = "x" * 101
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(server))
    result = validate(path)
    assert result.returncode == 1
    assert "PRIVATE_KEY_CANARY" not in result.stderr


@pytest.mark.parametrize(
    "readme",
    [
        "Install the published package: uv tool install solwyn-cli\n",
        "<!-- mcp-name: io.github.someone/solwyn -->\n",
        "mcp-name: ai.solwyn/solwyn-other\n",
    ],
)
def test_readme_must_carry_the_exact_ownership_marker(tmp_path, readme):
    spec = importlib.util.spec_from_file_location(
        "registry_validator", ROOT / "scripts/validate_mcp_registry.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    scratch = tmp_path / "checkout"
    scratch.mkdir()
    shutil.copytree(ROOT / "docs/registry", scratch / "docs/registry")
    shutil.copyfile(ROOT / "pyproject.toml", scratch / "pyproject.toml")
    shutil.copyfile(SERVER, scratch / "server.json")
    (scratch / "README.md").write_text(readme)
    validator.ROOT = scratch
    with pytest.raises(ValueError, match="ownership marker"):
        validator.validate_server(scratch / "server.json")
