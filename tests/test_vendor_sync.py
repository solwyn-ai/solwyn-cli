"""Tests for the explicit, local-only OpenAPI snapshot sync."""

import importlib.util
import json
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

COMMIT_OID = re.compile(r"[0-9a-f]{40}")


def _load_sync_module() -> ModuleType:
    script_path = Path("scripts/sync_spec.py").resolve()
    spec = importlib.util.spec_from_file_location("sync_spec_for_test", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _committed_checkout(
    checkout: Path, source_document: dict[str, object] | None = None
) -> str:
    """Build a synthetic Core checkout committed at a known, clean HEAD."""
    source = checkout / "shared" / "openapi.json"
    source.parent.mkdir(parents=True)
    document = source_document or {"openapi": "3.1.0", "paths": {"/health": {}}}
    source.write_text(json.dumps(document), encoding="utf-8")
    _git(checkout, "init")
    _git(checkout, "add", "shared/openapi.json")
    _git(
        checkout,
        "-c",
        "user.name=Snapshot Test",
        "-c",
        "user.email=snapshot@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "synthetic core checkout",
    )
    return _git(checkout, "rev-parse", "HEAD")


def _sync(
    checkout: Path, destination: Path, *flags: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "scripts/sync_spec.py",
            str(checkout),
            "--destination",
            str(destination),
            *flags,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_sync_spec_copies_local_schema_and_records_utc_date(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    source_document = {"openapi": "3.1.0", "paths": {"/health": {}}}
    _committed_checkout(checkout, source_document)
    date_before_sync = datetime.now(UTC).date().isoformat()

    result = _sync(checkout, destination)
    date_after_sync = datetime.now(UTC).date().isoformat()

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(destination.read_text(encoding="utf-8"))
    snapshot_date = snapshot.pop("x-solwyn-snapshot-date")
    snapshot.pop("x-solwyn-core-ref")
    assert snapshot == source_document
    assert snapshot_date in {date_before_sync, date_after_sync}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644


def test_sync_spec_records_clean_checkout_head_as_core_ref(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    head = _committed_checkout(checkout)

    result = _sync(checkout, destination)

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(destination.read_text(encoding="utf-8"))
    assert snapshot["x-solwyn-core-ref"] == head
    assert COMMIT_OID.fullmatch(snapshot["x-solwyn-core-ref"])


def test_sync_spec_refuses_dirty_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    _committed_checkout(checkout)
    (checkout / "uncommitted.py").write_text("dirty\n", encoding="utf-8")

    result = _sync(checkout, destination)

    assert result.returncode == 1
    assert "uncommitted changes" in result.stderr
    assert not destination.exists()


def test_sync_spec_records_null_core_ref_for_dirty_checkout_when_allowed(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    _committed_checkout(checkout)
    (checkout / "uncommitted.py").write_text("dirty\n", encoding="utf-8")

    result = _sync(checkout, destination, "--allow-dirty")

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(destination.read_text(encoding="utf-8"))
    assert snapshot["x-solwyn-core-ref"] is None


def test_sync_spec_refuses_checkout_without_git_history(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    source = checkout / "shared" / "openapi.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({"openapi": "3.1.0", "paths": {"/health": {}}}), encoding="utf-8"
    )

    result = _sync(checkout, destination)

    assert result.returncode == 1
    assert "git repository" in result.stderr
    assert not destination.exists()


def test_sync_spec_preserves_existing_destination_permissions(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    _committed_checkout(checkout, {"openapi": "3.1.0", "paths": {"/new": {}}})
    destination.write_text("existing snapshot\n", encoding="utf-8")
    destination.chmod(0o640)
    module = _load_sync_module()

    module.sync_spec(checkout, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_sync_spec_preserves_existing_snapshot_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "core"
    destination = tmp_path / "snapshot.json"
    _committed_checkout(checkout, {"openapi": "3.1.0", "paths": {"/new": {}}})
    destination.write_text("existing snapshot\n", encoding="utf-8")
    module = _load_sync_module()

    def fail_replace(_self: Path, _target: Path) -> Path:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        module.sync_spec(checkout, destination)

    assert destination.read_text(encoding="utf-8") == "existing snapshot\n"
    assert list(tmp_path.glob(".snapshot.json.*.tmp")) == []
