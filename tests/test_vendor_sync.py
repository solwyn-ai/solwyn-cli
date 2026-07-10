"""Tests for the explicit, local-only OpenAPI snapshot sync."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def test_sync_spec_copies_local_schema_and_records_utc_date(tmp_path: Path) -> None:
    checkout = tmp_path / "core"
    source = checkout / "shared" / "openapi.json"
    destination = tmp_path / "snapshot.json"
    source.parent.mkdir(parents=True)
    source_document = {"openapi": "3.1.0", "paths": {"/health": {}}}
    source.write_text(json.dumps(source_document), encoding="utf-8")

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

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(destination.read_text(encoding="utf-8"))
    assert snapshot == {
        **source_document,
        "x-solwyn-snapshot-date": datetime.now(UTC).date().isoformat(),
    }
