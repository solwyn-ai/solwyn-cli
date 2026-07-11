"""Verify that a Solwyn CLI wheel is complete and safe to distribute."""

import argparse
import re
import sys
import zipfile
from email import message_from_bytes
from pathlib import Path, PurePosixPath


EXPECTED_PACKAGE_FILES = {
    "solwyn_cli/__init__.py",
    "solwyn_cli/artifacts/__init__.py",
    "solwyn_cli/help.py",
    "solwyn_cli/mcp/server.py",
}
EXPECTED_RUNTIME_REQUIREMENTS = {
    "click",
    "httpx",
    "keyring",
    "mcp",
    "platformdirs",
    "pydantic",
    "rich",
    "tomli-w",
    "typer",
}
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
PATH_DEPENDENCY = re.compile(
    r"\s@\s|file:|(?:^|\s)(?:\.\.?/|/Users/|/workspace/)|"
    r"(?:path|workspace)\s*=|[A-Za-z]:\\",
    flags=re.IGNORECASE,
)


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def inspect_wheel(wheel_path: Path) -> None:
    """Raise ``ValueError`` when the built wheel is not release-ready."""
    if wheel_path.suffix != ".whl" or not wheel_path.is_file():
        raise ValueError(f"wheel not found: {wheel_path}")

    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            names = set(wheel.namelist())
            metadata_paths = sorted(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            if len(metadata_paths) != 1:
                raise ValueError(
                    "wheel must contain exactly one .dist-info/METADATA file"
                )
            metadata_bytes = wheel.read(metadata_paths[0])
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid wheel archive: {wheel_path}") from exc

    missing = EXPECTED_PACKAGE_FILES - names
    if missing:
        raise ValueError(f"missing expected package file: {sorted(missing)[0]}")

    forbidden = sorted(
        name
        for name in names
        if any(
            part.lower() == ".claude" or part.lower() == "claude.md"
            for part in PurePosixPath(name).parts
        )
    )
    if forbidden:
        raise ValueError(f"forbidden development file in wheel: {forbidden[0]}")

    metadata = message_from_bytes(metadata_bytes)
    if _normalized_name(metadata.get("Name", "")) != "solwyn-cli":
        raise ValueError("wheel metadata has an unexpected project name")

    requirements: set[str] = set()
    for requirement in metadata.get_all("Requires-Dist", []):
        if PATH_DEPENDENCY.search(requirement):
            raise ValueError("wheel metadata contains a path or workspace dependency")
        match = REQUIREMENT_NAME.match(requirement)
        if match is None:
            raise ValueError(f"invalid runtime requirement: {requirement}")
        requirements.add(_normalized_name(match.group(1)))

    missing_requirements = EXPECTED_RUNTIME_REQUIREMENTS - requirements
    unexpected_requirements = requirements - EXPECTED_RUNTIME_REQUIREMENTS
    if missing_requirements:
        raise ValueError(
            "wheel metadata is missing runtime requirement: "
            f"{sorted(missing_requirements)[0]}"
        )
    if unexpected_requirements:
        raise ValueError(
            "wheel metadata has unexpected runtime requirement: "
            f"{sorted(unexpected_requirements)[0]}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Path to exactly one built wheel")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        inspect_wheel(args.wheel)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Wheel metadata and contents verified", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
