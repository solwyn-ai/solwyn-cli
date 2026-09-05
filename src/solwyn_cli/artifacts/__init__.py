"""Single-source rendering and safe repository writes for agent guidance."""

import os
import secrets
import stat
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from solwyn_cli.artifacts._atomic import (
    atomic_exchange as _atomic_exchange,
    atomic_noreplace as _atomic_noreplace,
)


CLAUDE_SKILL_PATH = Path(".claude/skills/solwyn/SKILL.md")
CURSOR_RULE_PATH = Path(".cursor/rules/solwyn.mdc")
AGENTS_PATH = Path("AGENTS.md")

_CLAUDE_FRONTMATTER = (
    "---\nname: solwyn\ndescription: Use Solwyn budget guardrails safely.\n---\n\n"
)
_CURSOR_FRONTMATTER = (
    "---\n"
    "description: Apply Solwyn budget guardrails safely.\n"
    "alwaysApply: false\n"
    "---\n\n"
)
_AGENTS_BEGIN = "<!-- BEGIN SOLWYN AGENT CONTEXT -->"
_AGENTS_END = "<!-- END SOLWYN AGENT CONTEXT -->"
_TRANSACTION_PREFIX = ".solwyn-agent-"
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_IDENTITY = tuple[int, int, int, int, int, int]


class _ArtifactState(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    relative_path: Path
    parent_path: Path
    parent_fd: int
    parent_identity: tuple[int, int]
    target_name: str
    original_identity: _FILE_IDENTITY | None
    content: str
    temporary_name: str | None = None
    backup_name: str | None = None
    committed: bool = False
    installed_identity: _FILE_IDENTITY | None = None
    retain_backup_for_recovery: bool = False
    retain_temporary_for_recovery: bool = False
    rollback_recovery_name: str | None = None
    conflict_detected: bool = False


class ArtifactRecoveryError(OSError):
    """Rollback preserved concurrent edits or retained original recovery bytes."""

    def __init__(
        self,
        recovery_paths: tuple[Path, ...],
        conflict_paths: tuple[Path, ...] = (),
    ) -> None:
        self.recovery_paths = recovery_paths
        self.conflict_paths = conflict_paths
        details: list[str] = []
        if recovery_paths:
            details.append(
                "retained backup(s): " + ", ".join(str(path) for path in recovery_paths)
            )
        if conflict_paths:
            details.append(
                "preserved concurrent target(s): "
                + ", ".join(str(path) for path in conflict_paths)
            )
        super().__init__(
            "Agent artifact rollback failed; manual recovery required; "
            + "; ".join(details)
        )


class ArtifactSurface(StrEnum):
    """The four public surfaces backed by the same semantic guidance."""

    CLAUDE_SKILL = "claude_skill"
    CURSOR_RULE = "cursor_rule"
    AGENTS_SECTION = "agents_section"
    AGENT_CONTEXT = "agent_context"


def agent_guidance() -> str:
    """Return the canonical semantic source shared by every agent surface."""
    return """# Solwyn budget guardrails

Use Solwyn to observe and guard AI-provider spend without exposing credentials or duplicating reservations.

## Polling and reservations

- Use `solwyn budget status --json` for polling. It is a pure read and does not hold budget.
- Use `solwyn budget check` followed by `solwyn budget confirm` only around a specific un-instrumented provider spend that you make yourself.
- Never wrap SDK-instrumented code with `budget check` or `budget confirm`; the Solwyn SDK already reserves and confirms that spend.
- Fail-open is explicit: when a check returns `allowed=true` and `reservation_id=null`, skip confirm.
- Do not create repeated unconfirmed checks. They temporarily hold budget and can cause self-denial.

## Machine contract and free-tier backoff

- Use `--json` (or `--format json` / `SOLWYN_FORMAT=json`) for schema-bearing machine output. Success schema names are additive-only; JSON errors use `solwyn.error.v1` on stderr.
- Frozen exit codes are: 0 success, 1 generic/API, 2 usage, 3 auth, 4 budget denial, and 5 not found.
- On rate limits, honor `Retry-After` once for at most 10 seconds. On the free tier, cache and back off rather than creating a retry storm.

## Trust boundary

- Keep Solwyn credentials only in the OS keychain. Never echo them or place them in config files or argv.
- Solwyn must never collect, store, or proxy provider keys, prompts, or responses.
- The CLI sends only your own spend events; `sdk_instance_id` is a random per-invocation id; no telemetry.
- Do not add telemetry or update checks.
"""


def _normalized_guidance() -> str:
    return agent_guidance().rstrip("\n")


def render_surface(surface: ArtifactSurface) -> str:
    """Render one surface using only framing around the canonical guidance."""
    guidance = _normalized_guidance()
    if surface is ArtifactSurface.CLAUDE_SKILL:
        return f"{_CLAUDE_FRONTMATTER}{guidance}\n"
    if surface is ArtifactSurface.CURSOR_RULE:
        return f"{_CURSOR_FRONTMATTER}{guidance}\n"
    if surface is ArtifactSurface.AGENTS_SECTION:
        return f"{_AGENTS_BEGIN}\n{guidance}\n{_AGENTS_END}\n"
    return f"{guidance}\n"


def extract_guidance(surface: ArtifactSurface, content: str) -> str:
    """Extract semantic content for parity checks and downstream validation."""
    if surface is ArtifactSurface.CLAUDE_SKILL:
        if not content.startswith(_CLAUDE_FRONTMATTER):
            raise ValueError("Invalid Claude skill framing.")
        return content.removeprefix(_CLAUDE_FRONTMATTER).rstrip("\n")
    if surface is ArtifactSurface.CURSOR_RULE:
        if not content.startswith(_CURSOR_FRONTMATTER):
            raise ValueError("Invalid Cursor rule framing.")
        return content.removeprefix(_CURSOR_FRONTMATTER).rstrip("\n")
    if surface is ArtifactSurface.AGENTS_SECTION:
        prefix = f"{_AGENTS_BEGIN}\n"
        suffix = f"\n{_AGENTS_END}\n"
        if not content.startswith(prefix) or not content.endswith(suffix):
            raise ValueError("Invalid AGENTS section framing.")
        return content.removeprefix(prefix).removesuffix(suffix)
    return content.rstrip("\n")


def _fence(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped:
        return None
    character = stripped[0]
    if character not in {"`", "~"}:
        return None
    length = len(stripped) - len(stripped.lstrip(character))
    return (character, length) if length >= 3 else None


def _owned_marker_offsets(existing: str) -> tuple[list[int], list[tuple[int, int]]]:
    begins: list[int] = []
    ends: list[tuple[int, int]] = []
    open_fence: tuple[str, int] | None = None
    offset = 0
    for line in existing.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        fence = _fence(body)
        if open_fence is not None:
            if fence is not None and fence[0] == open_fence[0]:
                stripped = body.lstrip(" ")
                run_length = len(stripped) - len(stripped.lstrip(fence[0]))
                if run_length >= open_fence[1] and not stripped[run_length:].strip():
                    open_fence = None
        elif fence is not None:
            open_fence = fence
        elif body == _AGENTS_BEGIN:
            begins.append(offset)
        elif body == _AGENTS_END:
            ends.append((offset, offset + len(line)))
        offset += len(line)
    return begins, ends


def _merge_agents(existing: str, section: str) -> str:
    begins, ends = _owned_marker_offsets(existing)
    if not begins and not ends:
        if not existing:
            return section
        separator = "\n" if existing.endswith("\n") else "\n\n"
        return f"{existing}{separator}{section}"
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0][0]:
        raise ValueError("AGENTS.md contains malformed Solwyn section markers.")
    return f"{existing[: begins[0]]}{section}{existing[ends[0][1] :]}"


def _fsync_staged_file(file_fd: int) -> None:
    os.fsync(file_fd)


def _fsync_directory(directory_fd: int) -> None:
    os.fsync(directory_fd)


def _open_artifact_parent(
    repository: Path, relative_path: Path
) -> tuple[int, Path, tuple[int, int]]:
    current_path = repository
    current_fd = os.open(repository, _DIRECTORY_FLAGS)
    try:
        for component in relative_path.parent.parts:
            created = False
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
                created = True
            except FileExistsError:
                pass
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            if created:
                _fsync_directory(current_fd)
            os.close(current_fd)
            current_fd = next_fd
            current_path = current_path / component
        metadata = os.fstat(current_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"Agent artifact parent is not a directory: {current_path}")
        return current_fd, current_path, (metadata.st_dev, metadata.st_ino)
    except BaseException:
        os.close(current_fd)
        raise


def _file_identity(metadata: os.stat_result) -> _FILE_IDENTITY:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _target_identity(parent_fd: int, target_name: str) -> _FILE_IDENTITY | None:
    try:
        metadata = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _file_identity(metadata)


def _validate_initial_target(state: _ArtifactState) -> None:
    identity = state.original_identity
    if identity is not None and not stat.S_ISREG(identity[2]):
        raise OSError(
            f"Refusing non-regular agent artifact target: {state.relative_path}"
        )


def _read_agents(state: _ArtifactState) -> str:
    if state.original_identity is None:
        return ""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_fd = os.open(state.target_name, flags, dir_fd=state.parent_fd)
    try:
        if _file_identity(os.fstat(file_fd)) != state.original_identity:
            raise OSError(
                f"Agent artifact changed during agent artifact write: "
                f"{state.relative_path}"
            )
        with os.fdopen(file_fd, encoding="utf-8", newline="") as artifact_file:
            file_fd = -1
            return artifact_file.read()
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def _unique_transaction_name(kind: str, target_name: str) -> str:
    return f"{_TRANSACTION_PREFIX}{kind}-{target_name}-{secrets.token_hex(8)}"


def _unlink_at(parent_fd: int, name: str | None) -> None:
    if name is None:
        return
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _stage_artifact(state: _ArtifactState) -> str:
    temporary_name = _unique_transaction_name("stage", state.target_name)
    mode = 0o644
    if state.original_identity is not None:
        mode = stat.S_IMODE(state.original_identity[2]) & 0o777
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_fd = -1
    try:
        file_fd = os.open(
            temporary_name,
            flags,
            mode,
            dir_fd=state.parent_fd,
        )
        os.fchmod(file_fd, mode)
        with os.fdopen(
            file_fd, mode="w", encoding="utf-8", newline=""
        ) as temporary_file:
            file_fd = -1
            temporary_file.write(state.content)
            temporary_file.flush()
            _fsync_staged_file(temporary_file.fileno())
        return temporary_name
    except BaseException:
        if file_fd >= 0:
            os.close(file_fd)
        _unlink_at(state.parent_fd, temporary_name)
        raise


def _assert_parent_stable(state: _ArtifactState) -> None:
    descriptor_metadata = os.fstat(state.parent_fd)
    try:
        path_metadata = os.stat(state.parent_path, follow_symlinks=False)
    except FileNotFoundError:
        path_metadata = None
    descriptor_identity = (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
    path_identity = (
        (path_metadata.st_dev, path_metadata.st_ino)
        if path_metadata is not None and stat.S_ISDIR(path_metadata.st_mode)
        else None
    )
    if (
        descriptor_identity != state.parent_identity
        or path_identity != state.parent_identity
    ):
        raise OSError(
            f"Agent artifact parent changed during agent artifact write: "
            f"{state.relative_path.parent}"
        )


def _assert_target_stable(state: _ArtifactState) -> None:
    if _target_identity(state.parent_fd, state.target_name) != state.original_identity:
        raise OSError(
            f"Agent artifact changed during agent artifact write: {state.relative_path}"
        )


def _same_file_version(
    actual: _FILE_IDENTITY | None,
    expected: _FILE_IDENTITY | None,
) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return actual[:-1] == expected[:-1]


def _during_backup_copy(state: _ArtifactState) -> None:
    """Test seam after opening the stable source and before copying its bytes."""


def _write_all(file_fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(file_fd, remaining)
        if written == 0:
            raise OSError("Could not copy agent artifact backup.")
        remaining = remaining[written:]


def _backup_artifact(state: _ArtifactState) -> str | None:
    if state.original_identity is None:
        return None
    backup_name = _unique_transaction_name("backup", state.target_name)
    source_fd = -1
    backup_fd = -1
    try:
        source_flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        source_fd = os.open(
            state.target_name,
            source_flags,
            dir_fd=state.parent_fd,
        )
        if (
            _file_identity(os.fstat(source_fd)) != state.original_identity
            or _target_identity(state.parent_fd, state.target_name)
            != state.original_identity
        ):
            raise OSError(
                f"Agent artifact changed during agent artifact write: "
                f"{state.relative_path}"
            )

        backup_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        mode = stat.S_IMODE(state.original_identity[2]) & 0o777
        backup_fd = os.open(
            backup_name,
            backup_flags,
            mode,
            dir_fd=state.parent_fd,
        )
        os.fchmod(backup_fd, mode)
        _during_backup_copy(state)
        while chunk := os.read(source_fd, 128 * 1024):
            _write_all(backup_fd, chunk)
        _fsync_staged_file(backup_fd)

        source_identity = _file_identity(os.fstat(source_fd))
        backup_identity = _target_identity(state.parent_fd, backup_name)
        backup_descriptor_identity = _file_identity(os.fstat(backup_fd))
        if (
            source_identity != state.original_identity
            or _target_identity(state.parent_fd, state.target_name)
            != state.original_identity
            or backup_identity is None
            or backup_identity != backup_descriptor_identity
            or not stat.S_ISREG(backup_identity[2])
            or backup_identity[:2] == source_identity[:2]
            or backup_identity[3] != state.original_identity[3]
        ):
            raise OSError(
                f"Agent artifact changed during agent artifact write: "
                f"{state.relative_path}"
            )
        return backup_name
    except BaseException:
        _unlink_at(state.parent_fd, backup_name)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if backup_fd >= 0:
            os.close(backup_fd)


def _before_artifact_commit() -> None:
    """Test seam immediately before stable-parent and target revalidation."""


def _cleanup_transaction(states: list[_ArtifactState]) -> None:
    for state in states:
        if not state.retain_temporary_for_recovery:
            _unlink_at(state.parent_fd, state.temporary_name)
        if not state.retain_backup_for_recovery:
            _unlink_at(state.parent_fd, state.backup_name)


def _state_recovery_paths(state: _ArtifactState) -> list[Path]:
    names = []
    if state.retain_backup_for_recovery and state.backup_name is not None:
        names.append(state.backup_name)
    if state.retain_temporary_for_recovery and state.temporary_name is not None:
        names.append(state.temporary_name)
    if state.rollback_recovery_name is not None:
        names.append(state.rollback_recovery_name)
    return [state.relative_path.parent / name for name in names]


def _rollback_transaction(states: list[_ArtifactState]) -> None:
    rollback_error: OSError | None = None
    conflict_paths: list[Path] = [
        state.relative_path for state in states if state.conflict_detected
    ]
    for state in reversed(states):
        if not state.committed:
            continue
        if state.original_identity is not None and state.backup_name is not None:
            try:
                _atomic_exchange(
                    state.backup_name,
                    state.target_name,
                    state.parent_fd,
                )
            except OSError as error:
                state.retain_backup_for_recovery = True
                rollback_error = rollback_error or error
                continue
            displaced_identity = _target_identity(
                state.parent_fd,
                state.backup_name,
            )
            if _same_file_version(displaced_identity, state.installed_identity):
                _unlink_at(state.parent_fd, state.backup_name)
                state.backup_name = None
                _fsync_directory(state.parent_fd)
                continue
            try:
                _atomic_exchange(
                    state.backup_name,
                    state.target_name,
                    state.parent_fd,
                )
            except OSError as error:
                state.rollback_recovery_name = state.backup_name
                state.backup_name = None
                rollback_error = rollback_error or error
            else:
                state.retain_backup_for_recovery = True
            state.conflict_detected = True
            if state.relative_path not in conflict_paths:
                conflict_paths.append(state.relative_path)
            continue

        tombstone = _unique_transaction_name("rollback", state.target_name)
        try:
            _atomic_noreplace(state.target_name, tombstone, state.parent_fd)
        except FileNotFoundError:
            state.conflict_detected = True
            if state.relative_path not in conflict_paths:
                conflict_paths.append(state.relative_path)
            continue
        except OSError as error:
            rollback_error = rollback_error or error
            continue
        displaced_identity = _target_identity(state.parent_fd, tombstone)
        if _same_file_version(displaced_identity, state.installed_identity):
            _unlink_at(state.parent_fd, tombstone)
            _fsync_directory(state.parent_fd)
            continue
        try:
            _atomic_noreplace(tombstone, state.target_name, state.parent_fd)
        except OSError as error:
            state.rollback_recovery_name = tombstone
            rollback_error = rollback_error or error
        state.conflict_detected = True
        if state.relative_path not in conflict_paths:
            conflict_paths.append(state.relative_path)
    _cleanup_transaction(states)
    for state in states:
        try:
            _fsync_directory(state.parent_fd)
        except OSError as error:
            rollback_error = rollback_error or error
    recovery_paths = tuple(
        path for state in states for path in _state_recovery_paths(state)
    )
    ordered_conflicts = tuple(dict.fromkeys(conflict_paths))
    if recovery_paths or ordered_conflicts:
        raise ArtifactRecoveryError(
            recovery_paths,
            ordered_conflicts,
        ) from rollback_error
    if rollback_error is not None:
        raise OSError(
            "Could not roll back agent artifact transaction."
        ) from rollback_error


def _close_parents(states: list[_ArtifactState]) -> None:
    for state in states:
        os.close(state.parent_fd)


def write_agent_artifacts(root: Path) -> None:
    """Transactionally write artifacts using POSIX no-follow directory handles."""
    if os.name != "posix":
        raise OSError("Agent artifact writes require a POSIX filesystem.")
    repository = root.resolve(strict=True)
    rendered = {
        CLAUDE_SKILL_PATH: render_surface(ArtifactSurface.CLAUDE_SKILL),
        CURSOR_RULE_PATH: render_surface(ArtifactSurface.CURSOR_RULE),
    }
    states: list[_ArtifactState] = []
    commit_started = False
    try:
        parent_fd, parent_path, parent_identity = _open_artifact_parent(
            repository, AGENTS_PATH
        )
        agents_state = _ArtifactState(
            relative_path=AGENTS_PATH,
            parent_path=parent_path,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            target_name=AGENTS_PATH.name,
            original_identity=_target_identity(parent_fd, AGENTS_PATH.name),
            content="",
        )
        states.append(agents_state)
        _validate_initial_target(agents_state)
        agents_state.content = _merge_agents(
            _read_agents(agents_state),
            render_surface(ArtifactSurface.AGENTS_SECTION),
        )

        for relative_path in (CLAUDE_SKILL_PATH, CURSOR_RULE_PATH):
            parent_fd, parent_path, parent_identity = _open_artifact_parent(
                repository, relative_path
            )
            state = _ArtifactState(
                relative_path=relative_path,
                parent_path=parent_path,
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                target_name=relative_path.name,
                original_identity=_target_identity(parent_fd, relative_path.name),
                content=rendered.get(relative_path, ""),
            )
            states.append(state)
            _validate_initial_target(state)
        states = [states[1], states[2], states[0]]

        for state in states:
            state.temporary_name = _stage_artifact(state)
        for state in states:
            _fsync_directory(state.parent_fd)

        _before_artifact_commit()
        for state in states:
            _assert_parent_stable(state)
            _assert_target_stable(state)
            state.backup_name = _backup_artifact(state)
        for state in states:
            _fsync_directory(state.parent_fd)
        for state in states:
            _assert_parent_stable(state)
            _assert_target_stable(state)
        for state in states:
            commit_started = True
            if state.temporary_name is None:
                raise OSError("Agent artifact stage is missing.")
            staged_identity = _target_identity(
                state.parent_fd,
                state.temporary_name,
            )
            if staged_identity is None:
                raise OSError("Agent artifact stage is missing.")
            state.installed_identity = staged_identity
            if state.original_identity is None:
                try:
                    _atomic_noreplace(
                        state.temporary_name,
                        state.target_name,
                        state.parent_fd,
                    )
                except FileExistsError:
                    state.conflict_detected = True
                    raise OSError(
                        f"Agent artifact changed during agent artifact write: "
                        f"{state.relative_path}"
                    ) from None
                state.temporary_name = None
                state.committed = True
            else:
                _atomic_exchange(
                    state.temporary_name,
                    state.target_name,
                    state.parent_fd,
                )
                state.committed = True
                displaced_identity = _target_identity(
                    state.parent_fd,
                    state.temporary_name,
                )
                if not _same_file_version(
                    displaced_identity,
                    state.original_identity,
                ):
                    state.conflict_detected = True
                    state.retain_backup_for_recovery = True
                    try:
                        _atomic_exchange(
                            state.temporary_name,
                            state.target_name,
                            state.parent_fd,
                        )
                    except OSError:
                        state.retain_temporary_for_recovery = True
                    else:
                        state.committed = False
                    raise OSError(
                        f"Agent artifact changed during agent artifact write: "
                        f"{state.relative_path}"
                    )
            installed_identity = _target_identity(
                state.parent_fd,
                state.target_name,
            )
            if installed_identity is None or not _same_file_version(
                installed_identity, staged_identity
            ):
                raise OSError(
                    f"Agent artifact changed during agent artifact write: "
                    f"{state.relative_path}"
                )
            state.installed_identity = installed_identity
            if state.temporary_name is not None:
                _unlink_at(state.parent_fd, state.temporary_name)
                state.temporary_name = None
            _fsync_directory(state.parent_fd)
            _assert_parent_stable(state)
        _cleanup_transaction(states)
        for state in states:
            _fsync_directory(state.parent_fd)
    except BaseException:
        if commit_started:
            _rollback_transaction(states)
        else:
            _cleanup_transaction(states)
        raise
    finally:
        _close_parents(states)
