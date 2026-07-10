"""Strict golden assertions with readable diffs and no update mode."""

from difflib import unified_diff
from pathlib import Path


def assert_matches_golden(actual: str, golden_path: Path) -> None:
    """Compare text to a checked-in golden and show a unified diff on drift."""
    expected = golden_path.read_text(encoding="utf-8")
    if actual == expected:
        return
    diff = "".join(
        unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=str(golden_path),
            tofile="actual output",
        )
    )
    raise AssertionError(f"Golden output differs:\n{diff}")
