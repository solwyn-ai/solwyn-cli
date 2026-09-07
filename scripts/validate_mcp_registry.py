"""Validate the unpublished Registry draft offline, not namespace ownership."""

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


def validate_draft(path: Path) -> None:
    schema_bytes = SCHEMA_PATH.read_bytes()
    checksum = (SCHEMA_PATH.with_suffix(".sha256")).read_text().split()[0]
    if hashlib.sha256(schema_bytes).hexdigest() != checksum:
        raise ValueError("Registry schema checksum mismatch")
    schema = json.loads(schema_bytes)
    if schema["$id"] != SCHEMA_URL:
        raise ValueError("Unexpected Registry schema version")
    draft = json.loads(path.read_text())
    if list(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(draft)):
        raise ValueError("Registry draft does not satisfy the pinned schema")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if (
        draft.get("$schema") != SCHEMA_URL
        or draft["name"] != "ai.solwyn/solwyn"
        or draft["version"] != project["version"]
    ):
        raise ValueError("Draft namespace/schema/package version mismatch")
    if len(draft.get("packages", [])) != 1 or draft.get("remotes"):
        raise ValueError("Draft must describe one local package")
    package = draft["packages"][0]
    expected = {
        "registryType": "pypi",
        "registryBaseUrl": "https://pypi.org",
        "identifier": project["name"],
        "version": project["version"],
        "runtimeHint": "uvx",
        "transport": {"type": "stdio"},
    }
    if any(package.get(key) != value for key, value in expected.items()):
        raise ValueError("Draft package identity or transport mismatch")
    arguments = package.get("packageArguments", [])
    if (
        len(arguments) != 3
        or arguments[0].get("name") != "--project"
        or arguments[0].get("isRequired") is not True
        or arguments[1] != {"type": "positional", "value": "mcp"}
        or arguments[2] != {"type": "named", "name": "--profile", "value": "read"}
    ):
        raise ValueError("Draft must launch with global project and read profile")
    environment = package.get("environmentVariables", [])
    if (
        len(environment) != 1
        or environment[0].get("name") != "SOLWYN_API_KEY"
        or environment[0].get("isSecret") is not True
        or environment[0].get("isRequired") is not False
        or any(key in environment[0] for key in ("value", "default"))
    ):
        raise ValueError("Draft must declare an optional secret without a value")
    if (
        package.get("runtimeArguments")
        or set(project["scripts"]) != {"solwyn", "solwyn-cli"}
        or project["scripts"]["solwyn"] != project["scripts"]["solwyn-cli"]
    ):
        raise ValueError("Draft executable alias mismatch")
    readme = (ROOT / "README.md").read_text()
    if "not yet published to PyPI" not in readme or "<!-- mcp-name:" in readme:
        raise ValueError("Reassess draft state before preparing a published release")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--draft", type=Path, default=ROOT / "docs/registry/server.draft.json"
    )
    args = parser.parse_args()
    try:
        validate_draft(args.draft)
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "Registry draft validation failed; inspect shape and release metadata locally.",
            file=sys.stderr,
        )
        return 1
    print(
        "Unpublished draft shape and metadata verified; namespace ownership and package availability remain unverified.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
