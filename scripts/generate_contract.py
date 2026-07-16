"""Regenerate the committed machine-readable CLI contract snapshot.

Run this after any deliberate change to the CLI surface (commands, flags,
exit codes, or emitted JSON):

    uv run --no-sync python scripts/generate_contract.py

The snapshot is compared against the live CLI by
``tests/test_contract_snapshot.py``; drift fails CI until this is re-run and
the diff is reviewed.
"""

import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from contract.builder import SNAPSHOT_PATH, render_contract  # noqa: E402


def _atomic_write(destination: Path, serialized: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else 0o644
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_path.chmod(mode)
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(destination)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    _atomic_write(SNAPSHOT_PATH, render_contract())
    print(f"Wrote CLI contract snapshot to {SNAPSHOT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
