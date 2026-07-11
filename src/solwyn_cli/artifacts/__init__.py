"""Single-source rendering and safe repository writes for agent guidance."""

from enum import StrEnum
from pathlib import Path


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


def _merge_agents(existing: str, section: str) -> str:
    begin_count = existing.count(_AGENTS_BEGIN)
    end_count = existing.count(_AGENTS_END)
    if (begin_count, end_count) == (0, 0):
        if not existing:
            return section
        separator = "\n" if existing.endswith("\n") else "\n\n"
        return f"{existing}{separator}{section}"
    if (begin_count, end_count) != (1, 1):
        raise ValueError("AGENTS.md contains malformed Solwyn section markers.")
    start = existing.index(_AGENTS_BEGIN)
    end = existing.index(_AGENTS_END, start) + len(_AGENTS_END)
    replacement = section.rstrip("\n")
    return f"{existing[:start]}{replacement}{existing[end:]}"


def _validate_target(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise OSError(f"Refusing to write agent artifact through symlink: {path}")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_agent_artifacts(root: Path) -> None:
    """Write all repository artifacts deterministically and append AGENTS safely."""
    repository = root.resolve()
    paths = {
        relative_path: repository / relative_path
        for relative_path in (CLAUDE_SKILL_PATH, CURSOR_RULE_PATH, AGENTS_PATH)
    }
    for path in paths.values():
        _validate_target(repository, path)
    targets = {
        CLAUDE_SKILL_PATH: render_surface(ArtifactSurface.CLAUDE_SKILL),
        CURSOR_RULE_PATH: render_surface(ArtifactSurface.CURSOR_RULE),
    }
    agents_path = paths[AGENTS_PATH]
    existing_agents = (
        agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    )
    targets[AGENTS_PATH] = _merge_agents(
        existing_agents,
        render_surface(ArtifactSurface.AGENTS_SECTION),
    )
    for relative_path, content in targets.items():
        _write_text(paths[relative_path], content)
