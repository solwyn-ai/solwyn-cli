"""Validate server.json offline against the release metadata, not namespace ownership."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

from jsonschema import Draft7Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_URL = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)
SCHEMA_PATH = ROOT / "docs/registry/server.schema.2025-12-11.json"
SERVER_NAME = "ai.solwyn/solwyn"


def validate_server(path: Path) -> None:
    schema_bytes = SCHEMA_PATH.read_bytes()
    checksum = (SCHEMA_PATH.with_suffix(".sha256")).read_text().split()[0]
    if hashlib.sha256(schema_bytes).hexdigest() != checksum:
        raise ValueError("Registry schema checksum mismatch")
    schema = json.loads(schema_bytes)
    if schema["$id"] != SCHEMA_URL:
        raise ValueError("Unexpected Registry schema version")
    server = json.loads(path.read_text())
    if list(
        Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(server)
    ):
        raise ValueError("server.json does not satisfy the pinned schema")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if (
        server.get("$schema") != SCHEMA_URL
        or server["name"] != SERVER_NAME
        or server["version"] != project["version"]
    ):
        raise ValueError("server.json namespace/schema/package version mismatch")
    if len(server.get("packages", [])) != 1 or server.get("remotes"):
        raise ValueError("server.json must describe one local package")
    package = server["packages"][0]
    expected = {
        "registryType": "pypi",
        "registryBaseUrl": "https://pypi.org",
        "identifier": project["name"],
        "version": project["version"],
        "runtimeHint": "uvx",
        "transport": {"type": "stdio"},
    }
    if any(package.get(key) != value for key, value in expected.items()):
        raise ValueError("server.json package identity or transport mismatch")
    arguments = package.get("packageArguments", [])
    if (
        len(arguments) != 3
        or arguments[0].get("name") != "--project"
        or arguments[0].get("isRequired") is not True
        or arguments[1] != {"type": "positional", "value": "mcp"}
        or arguments[2] != {"type": "named", "name": "--profile", "value": "guardrails"}
    ):
        raise ValueError("server.json must launch with global project and guardrails")
    environment = package.get("environmentVariables", [])
    if (
        len(environment) != 1
        or environment[0].get("name") != "SOLWYN_API_KEY"
        or environment[0].get("isSecret") is not True
        or environment[0].get("isRequired") is not False
        or any(key in environment[0] for key in ("value", "default"))
    ):
        raise ValueError("server.json must declare an optional secret without a value")
    if (
        package.get("runtimeArguments")
        or set(project["scripts"]) != {"solwyn", "solwyn-cli"}
        or project["scripts"]["solwyn"] != project["scripts"]["solwyn-cli"]
    ):
        raise ValueError("server.json executable alias mismatch")
    readme = (ROOT / "README.md").read_text()
    if project.get("readme") != "README.md" or (
        f"<!-- mcp-name: {SERVER_NAME} -->" not in readme.splitlines()
    ):
        raise ValueError("README.md must carry the Registry ownership marker")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=ROOT / "server.json")
    args = parser.parse_args()
    try:
        validate_server(args.server)
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "server.json validation failed; inspect shape and release metadata locally.",
            file=sys.stderr,
        )
        return 1
    print(
        "server.json shape, README marker and release metadata verified; offline validation checks neither namespace ownership nor package availability.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
