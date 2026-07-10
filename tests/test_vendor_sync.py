"""Tests for the explicit, local-only OpenAPI snapshot sync."""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest


def _load_sync_module() -> ModuleType:
    script_path = Path("scripts/sync_spec.py").resolve()
    spec = importlib.util.spec_from_file_location("sync_spec_for_test", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_spec_copies_local_schema_and_records_utc_date(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    source = checkout / "shared" / "openapi.json"
    destination = tmp_path / "snapshot.json"
    source.parent.mkdir(parents=True)
    source_document = {"openapi": "3.1.0", "paths": {"/health": {}}}
    source.write_text(json.dumps(source_document), encoding="utf-8")
    date_before_sync = datetime.now(UTC).date().isoformat()

    result = subprocess.run(
        [
            sys.executable,
            "scripts/sync_spec.py",
            str(checkout),
            "--destination",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    date_after_sync = datetime.now(UTC).date().isoformat()

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(destination.read_text(encoding="utf-8"))
    snapshot_date = snapshot.pop("x-solwyn-snapshot-date")
    assert snapshot == source_document
    assert snapshot_date in {date_before_sync, date_after_sync}


def test_sync_spec_preserves_existing_snapshot_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "core"
    source = checkout / "shared" / "openapi.json"
    destination = tmp_path / "snapshot.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({"openapi": "3.1.0", "paths": {"/new": {}}}), encoding="utf-8"
    )
    destination.write_text("existing snapshot\n", encoding="utf-8")
    module = _load_sync_module()

    def fail_replace(_self: Path, _target: Path) -> Path:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        module.sync_spec(checkout, destination)

    assert destination.read_text(encoding="utf-8") == "existing snapshot\n"
    assert list(tmp_path.glob(".snapshot.json.*.tmp")) == []
