"""The repo file, ``<git root>/.solwyn.env`` (spec section 7.4)."""

import os
from pathlib import Path

import pytest

from solwyn_cli import repo_file

HEADER = "# Solwyn project for this repo. Not a secret: commit this file.\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout-bot"
    (root / ".git").mkdir(parents=True)
    return root


def test_render_is_exactly_the_header_and_one_double_quoted_assignment() -> None:
    assert repo_file.render("checkout bot") == (
        HEADER + 'SOLWYN_PROJECT="checkout bot"\n'
    )


@pytest.mark.parametrize("value", ['a"b', "a'b", "$HOME", "a`b", "a\\b", "a\nb"])
def test_render_refuses_values_a_project_name_never_holds(value: str) -> None:
    with pytest.raises(repo_file.RepoFileError):
        repo_file.render(value)


@pytest.mark.parametrize(
    "value", ["checkout-bot", "Checkout Bot 2", "proj_" + "ab" * 12, "a.b_c-d"]
)
def test_what_is_rendered_parses_back(value: str) -> None:
    assert repo_file.parse_repo_file(repo_file.render(value)) == value


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("checkout-bot", "checkout-bot"),
        ("My Repo!!(v2)", "My Repo-v2"),
        ("a/b\\c:d", "a-b-c-d"),
        ("--edge--", "edge"),
        ("  spaced  ", "spaced"),
        ("x" * 120, "x" * 100),
        ("x" * 99 + "!y", "x" * 99),
        ("", "project"),
        ("!!!", "project"),
        ("proj_" + "a" * 24, "repo-proj_" + "a" * 24),
        ("PROJ_" + "A" * 24, "repo-PROJ_" + "A" * 24),
        ("café", "caf"),
    ],
)
def test_sanitise_project_name(name: str, expected: str) -> None:
    assert repo_file.sanitise_project_name(name) == expected


def test_name_candidates_add_suffixes_and_stay_within_100() -> None:
    names = repo_file.name_candidates("x" * 100)

    assert names[0] == "x" * 100
    assert names[1] == "x" * 98 + "-2"
    assert names[-1] == "x" * 97 + "-99"
    assert len(names) == 99
    assert all(len(name) <= 100 for name in names)


def test_first_free_name_is_case_insensitive_and_ends_at_99() -> None:
    assert repo_file.first_free_name("svc", {"SVC", "svc-2"}) == "svc-3"
    taken = set(repo_file.name_candidates("svc"))
    assert repo_file.first_free_name("svc", taken) is None


def test_default_project_name_is_the_sanitised_git_root(repo: Path) -> None:
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)

    assert repo_file.default_project_name(nested) == "checkout-bot"
    assert repo_file.default_project_name(repo.parent) is None


def test_write_if_missing_writes_at_the_git_root(repo: Path) -> None:
    nested = repo / "src"
    nested.mkdir()

    outcome, path, existing = repo_file.write_if_missing("checkout-bot", nested)

    assert (outcome, path, existing) == ("written", repo / ".solwyn.env", None)
    assert path.read_text() == HEADER + 'SOLWYN_PROJECT="checkout-bot"\n'
    assert oct(path.stat().st_mode & 0o777) == oct(0o644 & ~_umask())
    assert [entry.name for entry in repo.iterdir() if entry.name.endswith(".tmp")] == []


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def test_write_if_missing_never_overwrites(repo: Path) -> None:
    path = repo / ".solwyn.env"
    path.write_text('SOLWYN_PROJECT="Other"\n')

    assert repo_file.write_if_missing("checkout-bot", repo) == ("other", path, "Other")
    assert repo_file.write_if_missing("other", repo) == ("same", path, "Other")
    assert path.read_text() == 'SOLWYN_PROJECT="Other"\n'


def test_write_if_missing_keeps_a_concurrent_writers_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    real_link = os.link

    def race(source: str | Path, target: str | Path) -> None:
        # Another process wrote the file between the check and the link.
        Path(target).write_text('SOLWYN_PROJECT="theirs"\n')
        real_link(source, target)

    monkeypatch.setattr(repo_file.os, "link", race)

    outcome, written, existing = repo_file.write_if_missing("mine", repo)

    assert (outcome, written, existing) == ("other", path, "theirs")
    assert path.read_text() == 'SOLWYN_PROJECT="theirs"\n'
    assert [entry.name for entry in repo.iterdir() if entry.name.endswith(".tmp")] == []


def test_write_if_missing_outside_a_repo_writes_nothing(tmp_path: Path) -> None:
    assert repo_file.write_if_missing("checkout-bot", tmp_path) == (
        "outside_repo",
        None,
        None,
    )
    assert not (tmp_path / ".solwyn.env").exists()


def test_rename_if_named_updates_only_a_file_naming_the_old_name(repo: Path) -> None:
    path = repo / ".solwyn.env"
    path.write_text("SOLWYN_PROJECT='Checkout-Bot'\n")

    assert repo_file.rename_if_named("other", "new-name", repo) is None
    assert path.read_text() == "SOLWYN_PROJECT='Checkout-Bot'\n"

    assert repo_file.rename_if_named("checkout-bot", "new-name", repo) == path
    assert path.read_text() == HEADER + 'SOLWYN_PROJECT="new-name"\n'


def test_rename_if_named_without_a_file_does_nothing(repo: Path) -> None:
    assert repo_file.rename_if_named("a", "b", repo) is None
    assert not (repo / ".solwyn.env").exists()


def test_rename_if_named_leaves_an_invalid_file_alone(repo: Path) -> None:
    path = repo / ".solwyn.env"
    path.write_text("SOLWYN_PROJECT=a\nOTHER=b\n")

    assert repo_file.rename_if_named("a", "b", repo) is None
    assert path.read_text() == "SOLWYN_PROJECT=a\nOTHER=b\n"


def test_the_repo_file_is_found_from_a_nested_directory(repo: Path) -> None:
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / ".solwyn.env").write_text(repo_file.render("checkout-bot"))

    assert repo_file.read_repo_project(nested) == ("checkout-bot", None)


def _hidden(repo: Path) -> list[str]:
    """Staged or moved-aside copies left beside the repo file."""
    return sorted(p.name for p in repo.iterdir() if p.name.startswith("..solwyn.env."))


def test_rename_keeps_an_edit_saved_after_the_old_name_was_read(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))
    real_render = repo_file.render

    def edit_then_render(value: str) -> str:
        # Another editor saves once rename_if_named has read the old name.
        path.write_text(real_render("research"))
        return real_render(value)

    monkeypatch.setattr(repo_file, "render", edit_then_render)

    with pytest.raises(repo_file.RepoFileConflictError) as raised:
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert raised.value.path == path
    assert raised.value.kept is None
    assert repo_file.parse_repo_file(path.read_text()) == "research"
    assert _hidden(repo) == []


def test_rename_keeps_a_file_written_while_the_old_one_is_moved_aside(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))
    real_rename = os.rename

    def rename_then_write(source: object, target: object) -> None:
        real_rename(source, target)
        if Path(source) == path:
            path.write_text(repo_file.render("research"))

    monkeypatch.setattr(os, "rename", rename_then_write)

    with pytest.raises(repo_file.RepoFileConflictError):
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert repo_file.parse_repo_file(path.read_text()) == "research"
    assert _hidden(repo) == []


def test_rename_keeps_a_write_through_an_already_open_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))
    real_rename, real_link = os.rename, os.link
    aside: list[Path] = []

    def record_rename(source: object, target: object) -> None:
        real_rename(source, target)
        aside.append(Path(target))

    def link_then_late_write(source: object, target: object) -> None:
        real_link(source, target)
        if Path(target) == path and aside:
            # An editor that opened the file earlier saves into it now.
            aside[0].write_text(repo_file.render("research"))

    monkeypatch.setattr(os, "rename", record_rename)
    monkeypatch.setattr(os, "link", link_then_late_write)

    with pytest.raises(repo_file.RepoFileConflictError) as raised:
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert raised.value.kept == aside[0]
    assert repo_file.parse_repo_file(aside[0].read_text()) == "research"
    assert _hidden(repo) == [aside[0].name]


# --- A failed or interrupted update puts the file back (audit F7) --------------


def _link_fails(
    monkeypatch: pytest.MonkeyPatch, path: Path, error: BaseException
) -> None:
    """The new content's link into ``path`` fails; other links still work."""
    real_link = os.link

    def link(source: object, target: object) -> None:
        if Path(target) == path and Path(source).name.endswith(".tmp"):
            raise error
        real_link(source, target)

    monkeypatch.setattr(os, "link", link)


@pytest.mark.parametrize(
    "error",
    [PermissionError(1, "Operation not permitted"), KeyboardInterrupt()],
    ids=["eperm", "ctrl-c"],
)
def test_a_failed_update_puts_the_old_file_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))
    _link_fails(monkeypatch, path, error)

    with pytest.raises(type(error)):
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert repo_file.parse_repo_file(path.read_text()) == "checkout-bot"
    assert _hidden(repo) == []


def test_without_hard_links_the_old_file_is_still_put_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))

    def no_links(source: object, target: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)

    with pytest.raises(PermissionError):
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert repo_file.parse_repo_file(path.read_text()) == "checkout-bot"
    assert _hidden(repo) == []


def test_a_file_written_during_a_failed_update_is_never_overwritten(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))
    real_link = os.link

    def link(source: object, target: object) -> None:
        if Path(target) == path and Path(source).name.endswith(".tmp"):
            # Someone writes the file while it is aside, then the link fails.
            path.write_text(repo_file.render("research"))
            raise PermissionError(1, "Operation not permitted")
        real_link(source, target)

    monkeypatch.setattr(os, "link", link)

    with pytest.raises(repo_file.RepoFileError) as raised:
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert repo_file.parse_repo_file(path.read_text()) == "research"
    (kept,) = _hidden(repo)
    assert kept in str(raised.value)
    assert repo_file.parse_repo_file((repo / kept).read_text()) == "checkout-bot"


def test_an_interrupt_after_the_new_file_names_the_copy_left_behind(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo / ".solwyn.env"
    path.write_text(repo_file.render("checkout-bot"))
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".old"):
            raise KeyboardInterrupt
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(KeyboardInterrupt) as raised:
        repo_file.rename_if_named("checkout-bot", "renamed", repo)

    assert repo_file.parse_repo_file(path.read_text()) == "renamed"
    (kept,) = _hidden(repo)
    assert any(kept in note for note in getattr(raised.value, "__notes__", []))
