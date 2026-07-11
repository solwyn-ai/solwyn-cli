"""Contract tests for the single-source agent artifacts."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli.artifacts import (
    AGENTS_PATH,
    CLAUDE_SKILL_PATH,
    CURSOR_RULE_PATH,
    ArtifactSurface,
    extract_guidance,
    render_surface,
    write_agent_artifacts,
)
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"


def test_all_four_surfaces_derive_from_one_canonical_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "# Sentinel guidance\n\nOne semantic source."
    monkeypatch.setattr("solwyn_cli.artifacts.agent_guidance", lambda: sentinel)

    rendered = {surface: render_surface(surface) for surface in ArtifactSurface}

    assert set(rendered) == set(ArtifactSurface)
    assert {
        extract_guidance(surface, content) for surface, content in rendered.items()
    } == {sentinel}


def test_artifact_content_teaches_guardrail_machine_and_trust_contracts() -> None:
    guidance = render_surface(ArtifactSurface.AGENT_CONTEXT)

    required = (
        "budget status",
        "polling",
        "un-instrumented",
        "budget check",
        "budget confirm",
        "Never wrap SDK-instrumented code",
        "reservation_id=null",
        "skip confirm",
        "--json",
        "solwyn.error.v1",
        "0 success",
        "1 generic/API",
        "2 usage",
        "3 auth",
        "4 budget denial",
        "5 not found",
        "Retry-After",
        "10 seconds",
        "free tier",
        "cache and back off",
        "OS keychain",
        "provider keys",
        "telemetry",
        "update checks",
    )
    for phrase in required:
        assert phrase in guidance


def test_renderers_add_only_deterministic_surface_framing() -> None:
    claude = render_surface(ArtifactSurface.CLAUDE_SKILL)
    cursor = render_surface(ArtifactSurface.CURSOR_RULE)
    agents = render_surface(ArtifactSurface.AGENTS_SECTION)

    assert claude.startswith(
        "---\nname: solwyn\ndescription: Use Solwyn budget guardrails safely.\n---\n\n"
    )
    assert cursor.startswith(
        "---\ndescription: Apply Solwyn budget guardrails safely.\nalwaysApply: false\n---\n\n"
    )
    assert agents.startswith("<!-- BEGIN SOLWYN AGENT CONTEXT -->\n")
    assert agents.endswith("\n<!-- END SOLWYN AGENT CONTEXT -->\n")
    assert extract_guidance(ArtifactSurface.CLAUDE_SKILL, claude) == extract_guidance(
        ArtifactSurface.CURSOR_RULE, cursor
    )
    assert extract_guidance(ArtifactSurface.AGENTS_SECTION, agents) == extract_guidance(
        ArtifactSurface.AGENT_CONTEXT,
        render_surface(ArtifactSurface.AGENT_CONTEXT),
    )


def test_write_agent_artifacts_creates_missing_directories_and_is_idempotent(
    tmp_path: Path,
) -> None:
    write_agent_artifacts(tmp_path)
    first = {
        path: (tmp_path / path).read_bytes()
        for path in (CLAUDE_SKILL_PATH, CURSOR_RULE_PATH, AGENTS_PATH)
    }

    write_agent_artifacts(tmp_path)

    assert {
        path: (tmp_path / path).read_bytes()
        for path in (CLAUDE_SKILL_PATH, CURSOR_RULE_PATH, AGENTS_PATH)
    } == first
    assert first[AGENTS_PATH].count(b"<!-- BEGIN SOLWYN AGENT CONTEXT -->") == 1


def test_write_agent_artifacts_appends_without_clobbering_existing_agents(
    tmp_path: Path,
) -> None:
    existing = "# Project agents\n\nKeep this exact instruction.\n"
    (tmp_path / AGENTS_PATH).write_text(existing, encoding="utf-8")

    write_agent_artifacts(tmp_path)

    rendered = (tmp_path / AGENTS_PATH).read_text(encoding="utf-8")
    assert rendered.startswith(existing)
    assert rendered.count("<!-- BEGIN SOLWYN AGENT CONTEXT -->") == 1
    assert "Keep this exact instruction." in rendered


def test_write_agent_artifacts_replaces_only_an_older_owned_section(
    tmp_path: Path,
) -> None:
    existing = (
        "before\n"
        "<!-- BEGIN SOLWYN AGENT CONTEXT -->\n"
        "stale owned guidance\n"
        "<!-- END SOLWYN AGENT CONTEXT -->\n"
        "after\n"
    )
    (tmp_path / AGENTS_PATH).write_text(existing, encoding="utf-8")

    write_agent_artifacts(tmp_path)

    rendered = (tmp_path / AGENTS_PATH).read_text(encoding="utf-8")
    assert rendered.startswith("before\n")
    assert rendered.endswith("after\n")
    assert "stale owned guidance" not in rendered
    assert rendered.count("<!-- BEGIN SOLWYN AGENT CONTEXT -->") == 1


def test_write_agent_artifacts_propagates_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(path: Path, content: str) -> None:
        raise OSError("read-only fixture")

    monkeypatch.setattr("solwyn_cli.artifacts._write_text", fail_write)

    with pytest.raises(OSError, match="read-only fixture"):
        write_agent_artifacts(tmp_path)


def test_agent_context_is_top_level_deterministic_stdout_without_auth_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("agent-context must not use auth or network")

    monkeypatch.setattr("solwyn_cli.auth.resolve_credential", forbidden)
    monkeypatch.setattr("solwyn_cli.client.SolwynClient", forbidden)

    first = runner.invoke(app, ["agent-context"])
    second = runner.invoke(app, ["agent-context"])
    root_help = runner.invoke(app, ["--help"])

    assert first.exit_code == 0, first.stderr
    assert second.exit_code == 0, second.stderr
    assert (
        first.stdout == second.stdout == render_surface(ArtifactSurface.AGENT_CONTEXT)
    )
    assert first.stderr == ""
    assert "agent-context" in root_help.stdout


def test_agent_context_matches_stable_golden() -> None:
    result = runner.invoke(app, ["agent-context"])

    assert result.exit_code == 0, result.stderr
    assert_matches_golden(result.stdout, GOLDEN_DIR / "agent-context.txt")
    assert result.stderr == ""
