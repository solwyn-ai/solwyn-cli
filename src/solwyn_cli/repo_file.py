"""The repo file: ``<git root>/.solwyn.env`` names the repo's Solwyn project.

It is not a secret and is meant to be committed. The CLI writes exactly::

    # Solwyn project for this repo. Not a secret: commit this file.
    SOLWYN_PROJECT="<name>"

and reads that one assignment, with optional surrounding whitespace, optional
quotes, comments and blank lines; anything else is ``repo_file_invalid``. It
never holds a key. The CLI writes it only when it is missing and never
overwrites it silently; a rename of the project it names updates it in place.
"""

import os
import re
import secrets
from pathlib import Path
from typing import Literal

from solwyn_cli._vendor.validation import trim_selector

REPO_FILE_NAME = ".solwyn.env"
REPO_VARIABLE = "SOLWYN_PROJECT"
HEADER = "# Solwyn project for this repo. Not a secret: commit this file."
PROJECT_NAME_MAX_LENGTH = 100
_OUTSIDE_NAME = re.compile(r"[^A-Za-z0-9 ._-]")
_DASH_RUNS = re.compile(r"-{2,}")
_ID_SHAPE = re.compile(r"^proj_[a-f0-9]{24}$", re.IGNORECASE)


class RepoFileError(ValueError):
    """``.solwyn.env`` exists but is not the one assignment it may hold."""


def git_root(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` that holds ``.git``."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def repo_root(start: Path | None = None) -> Path | None:
    """The git root of ``start`` (cwd), resolved; None outside a repo."""
    try:
        here = (start or Path.cwd()).resolve()
    except OSError:
        return None
    return git_root(here)


def find_repo_file(start: Path | None = None) -> Path | None:
    """``.solwyn.env`` from ``start`` (cwd) up to the git root, if any."""
    try:
        here = (start or Path.cwd()).resolve()
    except OSError:
        return None
    root = git_root(here)
    if root is None:
        return None
    for directory in (here, *here.parents):
        candidate = directory / REPO_FILE_NAME
        if candidate.is_file():
            return candidate
        if directory == root:
            break
    return None


def parse_repo_file(text: str) -> str:
    """The ``SOLWYN_PROJECT`` value of a repo file, or RepoFileError.

    Accepts that one assignment (optional surrounding whitespace, optional
    single or double quotes), comments and blank lines; nothing else.
    """
    value: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw = stripped.partition("=")
        if not separator or name.strip() != REPO_VARIABLE or value is not None:
            raise RepoFileError("unexpected line")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        elif raw[:1] in "\"'" or raw[-1:] in "\"'":
            raise RepoFileError("unbalanced quotes")
        raw = trim_selector(raw)
        if not raw or any(character in raw for character in "\"'$`\\"):
            raise RepoFileError("invalid value")
        value = raw
    if value is None:
        raise RepoFileError("no assignment")
    return value


def read_repo_project(start: Path | None = None) -> tuple[str | None, str | None]:
    """(value, None), (None, error message) or (None, None) without a file."""
    path = find_repo_file(start)
    if path is None:
        return None, None
    try:
        return parse_repo_file(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, RepoFileError):
        return None, (
            f'{path} must hold one line, SOLWYN_PROJECT="<project name or '
            'proj_ id>", plus comments.'
        )


def render(value: str) -> str:
    """The file's exact content for one project name or id."""
    if any(character in value for character in "\"'$`\\\n\r"):
        raise RepoFileError("A project name never holds quotes, $, ` or \\.")
    return f'{HEADER}\n{REPO_VARIABLE}="{value}"\n'


def _publish(path: Path, content: str, *, replace: bool) -> None:
    """Write a temporary file beside ``path``, then link (new) or replace it."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # A hard link never replaces an existing file: a concurrent
            # writer's file is kept and this one reports "exists".
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


WriteOutcome = Literal["written", "same", "other", "outside_repo"]


def write_if_missing(
    value: str, start: Path | None = None
) -> tuple[WriteOutcome, Path | None, str | None]:
    """Write ``<git root>/.solwyn.env`` naming ``value`` unless one exists.

    Returns the outcome, the file's path and, for ``other``, the value the
    existing file names. An existing file is never overwritten.
    """
    root = repo_root(start)
    if root is None:
        return "outside_repo", None, None
    path = root / REPO_FILE_NAME
    existing, _ = read_repo_project(root) if path.exists() else (None, None)
    if path.exists():
        if existing is not None and existing.casefold() == value.casefold():
            return "same", path, existing
        return "other", path, existing
    try:
        _publish(path, render(value), replace=False)
    except FileExistsError:
        existing, _ = read_repo_project(root)
        if existing is not None and existing.casefold() == value.casefold():
            return "same", path, existing
        return "other", path, existing
    return "written", path, None


def rename_if_named(old: str, new: str, start: Path | None = None) -> Path | None:
    """Point the repo file at ``new`` when it names ``old``; its path, or None."""
    path = find_repo_file(start)
    if path is None:
        return None
    value, error = read_repo_project(path.parent)
    if error is not None or value is None or value.casefold() != old.casefold():
        return None
    _publish(path, render(new), replace=True)
    return path


def sanitise_project_name(name: str) -> str:
    """A repo directory's name as a valid project name (spec section 7.4).

    Characters outside ``[A-Za-z0-9 ._-]`` become ``-``, runs of ``-``
    collapse, spaces and ``-`` are stripped at both ends, the result is cut to
    100, an empty result becomes ``project``, and a ``proj_``-shaped one gets
    the prefix ``repo-``.
    """
    cleaned = _DASH_RUNS.sub("-", _OUTSIDE_NAME.sub("-", name)).strip(" -")
    cleaned = cleaned[:PROJECT_NAME_MAX_LENGTH].strip(" -")
    if not cleaned:
        cleaned = "project"
    if _ID_SHAPE.fullmatch(cleaned):
        cleaned = f"repo-{cleaned}"[:PROJECT_NAME_MAX_LENGTH]
    return cleaned


def default_project_name(start: Path | None = None) -> str | None:
    """The sanitised name of the repo's root directory; None outside a repo."""
    root = repo_root(start)
    return None if root is None else sanitise_project_name(root.name)


def name_candidates(base: str) -> list[str]:
    """``base``, then ``base-2`` to ``base-99``, each cut to fit 100 characters."""
    names = [base]
    for number in range(2, 100):
        suffix = f"-{number}"
        stem = base[: PROJECT_NAME_MAX_LENGTH - len(suffix)].rstrip(" -") or "project"
        names.append(f"{stem}{suffix}")
    return names


def first_free_name(base: str, taken: set[str]) -> str | None:
    """The first candidate no active project uses (case-insensitively)."""
    folded = {name.casefold() for name in taken}
    return next(
        (name for name in name_candidates(base) if name.casefold() not in folded),
        None,
    )
