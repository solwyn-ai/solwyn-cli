"""Contract tests for the single-source agent artifacts."""

import os
import re
from pathlib import Path

import pytest
from golden.harness import assert_matches_golden
from typer.testing import CliRunner

from solwyn_cli import artifacts as artifacts_module
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
REPOSITORY_ROOT = Path(__file__).parent.parent
ARTIFACT_PATHS = (CLAUDE_SKILL_PATH, CURSOR_RULE_PATH, AGENTS_PATH)


def _artifact_snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path: (root / path).read_bytes()
        for path in ARTIFACT_PATHS
        if (root / path).exists()
    }


def _transaction_residue(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.name.startswith(".solwyn-agent-")]


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
        "credential store (solwyn login)",
        "provider keys",
        "telemetry",
        "update checks",
    )
    for phrase in required:
        assert phrase in guidance


def test_artifact_content_teaches_sdk_run_and_attribution_contracts() -> None:
    guidance = render_surface(ArtifactSurface.AGENT_CONTEXT)

    required = (
        "same reserve, settle and record loop as the Solwyn SDKs",
        "SOLWYN_AGENT_RUN_ID",
        "Reuse one run ID for the entire job",
        "run_terminated",
        "do not retry under a new run ID",
        "solwyn runs stop RUN",
        "in-flight requests finish",
        "enforcement_deadline",
        "SOLWYN_TAGS",
        "split on the first `=`",
        "do not trim whitespace",
        "last duplicate key wins",
        "solwyn record",
        "after the caller-made provider call",
        "canonical lowercase UUID",
        "Reuse that same call ID",
        "records attribution after settlement by default",
        "Do not also run `solwyn record`",
        "--no-record",
        "relative price hints",
    )
    for phrase in required:
        assert phrase in guidance


def test_artifact_content_reserves_guardrail_changes_for_a_human() -> None:
    for surface in ArtifactSurface:
        guidance = render_surface(surface)

        required = (
            "Never raise a budget cap.",
            "Never switch enforcement from `hard_deny` to `alert_only`.",
            "Never pause or remove budget rules.",
            "Never rotate or revoke keys, and never change billing or the plan.",
            "stop and ask the human",
            "meters the calls your code makes",
            "does not meter this coding assistant's own model usage",
        )
        for phrase in required:
            assert phrase in guidance, (surface, phrase)


def test_guidance_and_readme_name_no_sdk_version_that_can_go_stale() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    for text in (readme, *(render_surface(surface) for surface in ArtifactSurface)):
        assert re.search(r"SDK v?\d", text) is None


def test_readme_frames_sdk_enforcement_attribution_and_mcp_capabilities() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "enforce and attribute LLM spend" in readme
    assert "same reserve, settle and record loop as the Solwyn SDKs" in readme
    assert "covers enforcement and attribution" in readme
    assert "MCP capability and schema discovery" in readme
    assert "serves read, guardrail, and attribution tools" in readme
    assert "serves read, guardrail, and attribution tools over stdio —" not in readme


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


@pytest.mark.parametrize(
    "existing",
    [b"# Project agents\r\n\r\nKeep CRLF bytes.\r\n", b"no trailing newline"],
    ids=("crlf", "no-trailing-newline"),
)
def test_write_agent_artifacts_preserves_existing_agents_prefix_bytes(
    tmp_path: Path, existing: bytes
) -> None:
    (tmp_path / AGENTS_PATH).write_bytes(existing)

    write_agent_artifacts(tmp_path)

    assert (tmp_path / AGENTS_PATH).read_bytes().startswith(existing)


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


def test_inline_and_fenced_marker_examples_are_preserved_and_do_not_claim_ownership(
    tmp_path: Path,
) -> None:
    existing = (
        "Inline <!-- BEGIN SOLWYN AGENT CONTEXT --> example.\n\n"
        "```markdown\n"
        "<!-- BEGIN SOLWYN AGENT CONTEXT -->\n"
        "example body\n"
        "<!-- END SOLWYN AGENT CONTEXT -->\n"
        "```\n"
    )
    (tmp_path / AGENTS_PATH).write_text(existing, encoding="utf-8")

    write_agent_artifacts(tmp_path)

    rendered = (tmp_path / AGENTS_PATH).read_text(encoding="utf-8")
    assert rendered.startswith(existing)
    assert "example body" in rendered
    assert rendered.endswith(render_surface(ArtifactSurface.AGENTS_SECTION))


@pytest.mark.parametrize(
    "existing",
    [
        (
            "<!-- END SOLWYN AGENT CONTEXT -->\n"
            "body\n"
            "<!-- BEGIN SOLWYN AGENT CONTEXT -->\n"
        ),
        (
            "<!-- BEGIN SOLWYN AGENT CONTEXT -->\n"
            "one\n"
            "<!-- BEGIN SOLWYN AGENT CONTEXT -->\n"
            "two\n"
            "<!-- END SOLWYN AGENT CONTEXT -->\n"
        ),
        "<!-- BEGIN SOLWYN AGENT CONTEXT -->\nincomplete\n",
        "<!-- END SOLWYN AGENT CONTEXT -->\nincomplete\n",
    ],
    ids=("reversed", "duplicate", "missing-end", "missing-begin"),
)
def test_malformed_standalone_owned_markers_fail_without_writes(
    tmp_path: Path, existing: str
) -> None:
    agents = tmp_path / AGENTS_PATH
    agents.write_text(existing, encoding="utf-8")
    before = agents.read_bytes()

    with pytest.raises(ValueError, match="malformed Solwyn section markers"):
        write_agent_artifacts(tmp_path)

    assert agents.read_bytes() == before
    assert not (tmp_path / CLAUDE_SKILL_PATH).exists()
    assert not (tmp_path / CURSOR_RULE_PATH).exists()
    assert _transaction_residue(tmp_path) == []


def test_mid_stage_fsync_failure_preserves_all_originals_and_removes_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    monkeypatch.setattr(
        artifacts_module,
        "_fsync_staged_file",
        lambda _fd: (_ for _ in ()).throw(OSError("injected stage failure")),
    )

    with pytest.raises(OSError, match="injected stage failure"):
        write_agent_artifacts(tmp_path)

    assert _artifact_snapshot(tmp_path) == before
    assert _transaction_residue(tmp_path) == []


def test_second_target_stage_failure_preserves_all_originals_and_removes_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    original_stage = artifacts_module._stage_artifact
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second stage failure")
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(artifacts_module, "_stage_artifact", fail_second)

    with pytest.raises(OSError, match="injected second stage failure"):
        write_agent_artifacts(tmp_path)

    assert _artifact_snapshot(tmp_path) == before
    assert _transaction_residue(tmp_path) == []


def test_second_commit_failure_rolls_back_every_original_and_removes_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    monkeypatch.setattr(
        artifacts_module,
        "agent_guidance",
        lambda: "# Changed guidance\n\nRollback must restore different bytes.",
    )
    original_replace = artifacts_module._atomic_exchange
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected commit failure")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(artifacts_module, "_atomic_exchange", fail_second)

    with pytest.raises(OSError, match="injected commit failure"):
        write_agent_artifacts(tmp_path)

    assert _artifact_snapshot(tmp_path) == before
    assert _transaction_residue(tmp_path) == []


@pytest.mark.parametrize(
    "mutated_path",
    [CURSOR_RULE_PATH, AGENTS_PATH],
    ids=("second-target", "third-target"),
)
def test_target_edit_between_commit_iterations_survives_and_prior_commits_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutated_path: Path,
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    user_bytes = f"concurrent edit of {mutated_path}\n".encode()
    monkeypatch.setattr(
        artifacts_module,
        "agent_guidance",
        lambda: "# Changed guidance\n\nPrior commits must really roll back.",
    )
    original_exchange = artifacts_module._atomic_exchange
    mutated = False

    def mutate_at_exchange(source: str, target: str, parent_fd: int) -> None:
        nonlocal mutated
        if target == mutated_path.name and not mutated:
            (tmp_path / mutated_path).write_bytes(user_bytes)
            mutated = True
        original_exchange(source, target, parent_fd)

    monkeypatch.setattr(
        artifacts_module,
        "_atomic_exchange",
        mutate_at_exchange,
    )

    with pytest.raises(
        artifacts_module.ArtifactRecoveryError,
        match="manual recovery required",
    ) as captured:
        write_agent_artifacts(tmp_path)

    for path, original in before.items():
        expected = user_bytes if path == mutated_path else original
        assert (tmp_path / path).read_bytes() == expected
    residue = _transaction_residue(tmp_path)
    assert len(residue) == 1
    assert residue[0].read_bytes() == before[mutated_path]
    assert captured.value.recovery_paths == (mutated_path.parent / residue[0].name,)
    assert captured.value.conflict_paths == (mutated_path,)


def test_new_target_created_between_commit_iterations_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_bytes = b"concurrent new cursor rule\n"
    original_noreplace = artifacts_module._atomic_noreplace
    created = False

    def create_at_noreplace(source: str, target: str, parent_fd: int) -> None:
        nonlocal created
        if target == CURSOR_RULE_PATH.name and not created:
            (tmp_path / CURSOR_RULE_PATH).write_bytes(user_bytes)
            created = True
        original_noreplace(source, target, parent_fd)

    monkeypatch.setattr(
        artifacts_module,
        "_atomic_noreplace",
        create_at_noreplace,
    )

    with pytest.raises(
        artifacts_module.ArtifactRecoveryError,
        match="manual recovery required",
    ) as captured:
        write_agent_artifacts(tmp_path)

    assert not (tmp_path / CLAUDE_SKILL_PATH).exists()
    assert (tmp_path / CURSOR_RULE_PATH).read_bytes() == user_bytes
    assert not (tmp_path / AGENTS_PATH).exists()
    assert captured.value.recovery_paths == ()
    assert captured.value.conflict_paths == (CURSOR_RULE_PATH,)
    assert _transaction_residue(tmp_path) == []


def test_post_commit_user_edit_survives_with_original_backup_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    user_bytes = b"user edit after transaction replacement\n"
    monkeypatch.setattr(
        artifacts_module,
        "agent_guidance",
        lambda: "# Changed guidance\n\nTrigger a partial commit.",
    )
    original_exchange = artifacts_module._atomic_exchange
    calls = 0

    def mutate_during_rollback(source: str, target: str, parent_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second commit failure")
        if calls == 3:
            (tmp_path / CLAUDE_SKILL_PATH).write_bytes(user_bytes)
        original_exchange(source, target, parent_fd)

    monkeypatch.setattr(
        artifacts_module,
        "_atomic_exchange",
        mutate_during_rollback,
    )

    with pytest.raises(
        artifacts_module.ArtifactRecoveryError,
        match="manual recovery required",
    ) as captured:
        write_agent_artifacts(tmp_path)

    assert (tmp_path / CLAUDE_SKILL_PATH).read_bytes() == user_bytes
    assert (tmp_path / CURSOR_RULE_PATH).read_bytes() == before[CURSOR_RULE_PATH]
    assert (tmp_path / AGENTS_PATH).read_bytes() == before[AGENTS_PATH]
    residue = _transaction_residue(tmp_path)
    assert len(residue) == 1
    recovery_backup = residue[0]
    assert recovery_backup.read_bytes() == before[CLAUDE_SKILL_PATH]
    assert captured.value.recovery_paths == (
        CLAUDE_SKILL_PATH.parent / recovery_backup.name,
    )
    assert captured.value.conflict_paths == (CLAUDE_SKILL_PATH,)


def test_post_commit_edit_of_previously_absent_target_survives_without_fake_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_bytes = b"user edit of newly installed skill\n"
    original_noreplace = artifacts_module._atomic_noreplace
    calls = 0

    def mutate_during_rollback(source: str, target: str, parent_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second commit failure")
        if calls == 3:
            (tmp_path / CLAUDE_SKILL_PATH).write_bytes(user_bytes)
        original_noreplace(source, target, parent_fd)

    monkeypatch.setattr(
        artifacts_module,
        "_atomic_noreplace",
        mutate_during_rollback,
    )

    with pytest.raises(
        artifacts_module.ArtifactRecoveryError,
        match="manual recovery required",
    ) as captured:
        write_agent_artifacts(tmp_path)

    assert (tmp_path / CLAUDE_SKILL_PATH).read_bytes() == user_bytes
    assert not (tmp_path / CURSOR_RULE_PATH).exists()
    assert not (tmp_path / AGENTS_PATH).exists()
    assert captured.value.recovery_paths == ()
    assert captured.value.conflict_paths == (CLAUDE_SKILL_PATH,)
    assert _transaction_residue(tmp_path) == []


def test_failed_restore_retains_only_recoverable_original_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    original_path = tmp_path / CLAUDE_SKILL_PATH
    original_fd = os.open(
        original_path,
        os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
    )
    original_inode = os.fstat(original_fd).st_ino
    monkeypatch.setattr(
        artifacts_module,
        "agent_guidance",
        lambda: "# Changed guidance\n\nA failed restore must retain the original.",
    )
    original_replace = artifacts_module._atomic_exchange
    calls = 0

    def fail_commit_then_restore(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected commit failure")
        if calls == 3:
            raise OSError("injected restore failure")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(
        artifacts_module,
        "_atomic_exchange",
        fail_commit_then_restore,
    )

    try:
        with pytest.raises(OSError, match="manual recovery required") as captured:
            write_agent_artifacts(tmp_path)

        assert type(captured.value).__name__ == "ArtifactRecoveryError"
        assert (tmp_path / CLAUDE_SKILL_PATH).read_bytes() != before[CLAUDE_SKILL_PATH]
        assert (tmp_path / CURSOR_RULE_PATH).read_bytes() == before[CURSOR_RULE_PATH]
        assert (tmp_path / AGENTS_PATH).read_bytes() == before[AGENTS_PATH]
        residue = _transaction_residue(tmp_path)
        assert len(residue) == 1
        recovery_backup = residue[0]
        assert recovery_backup.name.startswith(".solwyn-agent-backup-SKILL.md-")
        assert recovery_backup.read_bytes() == before[CLAUDE_SKILL_PATH]
        assert recovery_backup.stat().st_ino != original_inode
        assert getattr(captured.value, "recovery_paths") == (
            CLAUDE_SKILL_PATH.parent / recovery_backup.name,
        )

        mutation = b"mutated through held original descriptor"
        os.lseek(original_fd, 0, os.SEEK_SET)
        os.write(original_fd, mutation)
        os.ftruncate(original_fd, len(mutation))

        assert recovery_backup.read_bytes() == before[CLAUDE_SKILL_PATH]
    finally:
        os.close(original_fd)


def test_source_change_during_backup_copy_fails_closed_without_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)

    def mutate_source(state: object) -> None:
        relative_path = getattr(state, "relative_path")
        if relative_path == CLAUDE_SKILL_PATH:
            (tmp_path / relative_path).write_text(
                "concurrent source mutation\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(artifacts_module, "_during_backup_copy", mutate_source)

    with pytest.raises(OSError, match="changed during agent artifact write"):
        write_agent_artifacts(tmp_path)

    assert (tmp_path / CLAUDE_SKILL_PATH).read_text(encoding="utf-8") == (
        "concurrent source mutation\n"
    )
    assert (tmp_path / CURSOR_RULE_PATH).read_bytes() == before[CURSOR_RULE_PATH]
    assert (tmp_path / AGENTS_PATH).read_bytes() == before[AGENTS_PATH]
    assert _transaction_residue(tmp_path) == []


def test_target_symlink_swap_before_commit_never_writes_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    target = tmp_path / AGENTS_PATH
    saved = tmp_path / "AGENTS.saved"
    outside = tmp_path.parent / "outside-agents.md"
    outside.write_text("outside must stay exact\n", encoding="utf-8")

    def swap_target() -> None:
        target.rename(saved)
        target.symlink_to(outside)

    monkeypatch.setattr(artifacts_module, "_before_artifact_commit", swap_target)

    with pytest.raises(OSError, match="changed during agent artifact write"):
        write_agent_artifacts(tmp_path)

    assert outside.read_text(encoding="utf-8") == "outside must stay exact\n"
    assert saved.read_bytes() == before[AGENTS_PATH]
    assert _transaction_residue(tmp_path) == []


def test_parent_symlink_swap_before_commit_never_writes_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent_artifacts(tmp_path)
    before = _artifact_snapshot(tmp_path)
    parent = tmp_path / CURSOR_RULE_PATH.parent
    saved_parent = parent.with_name("rules.saved")
    outside = tmp_path.parent / "outside-rules"
    outside.mkdir()
    outside_rule = outside / CURSOR_RULE_PATH.name
    outside_rule.write_text("outside must stay exact\n", encoding="utf-8")

    def swap_parent() -> None:
        parent.rename(saved_parent)
        parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(artifacts_module, "_before_artifact_commit", swap_parent)

    with pytest.raises(OSError, match="parent changed during agent artifact write"):
        write_agent_artifacts(tmp_path)

    assert outside_rule.read_text(encoding="utf-8") == "outside must stay exact\n"
    assert (saved_parent / CURSOR_RULE_PATH.name).read_bytes() == before[
        CURSOR_RULE_PATH
    ]
    assert _transaction_residue(tmp_path) == []


def test_write_agent_artifacts_propagates_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(state: object) -> str:
        raise OSError("read-only fixture")

    monkeypatch.setattr("solwyn_cli.artifacts._stage_artifact", fail_write)

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


def test_guidance_keeps_the_agent_on_this_repos_project_and_key() -> None:
    from solwyn_cli.artifacts import agent_guidance

    text = agent_guidance()

    assert "This repo's Solwyn project is set in .solwyn.env (SOLWYN_PROJECT)." in text
    assert "Do not change it to reach another project's budget." in text
    assert 'eval "$(solwyn keys env)"' in text
    assert "Never point SOLWYN_PROJECT or `--project` at another project" in text
    assert "a change a person makes in the dashboard" in text
    # Never a CLI route to widen a key.
    for forbidden in ("add-project", "keys add", "remove-project", "—"):
        assert forbidden not in text
