"""Copy the customer OpenAPI contract from a local Core checkout."""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "solwyn_cli"
    / "spec"
    / "openapi_snapshot.json"
)
_COMMIT_OID = re.compile(r"[0-9a-f]{40}")


def _read_source(source_path: Path) -> dict[str, Any]:
    if not source_path.is_file():
        raise ValueError(f"OpenAPI source not found: {source_path}")

    try:
        document = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"OpenAPI source is not valid JSON: {source_path}") from exc

    if not isinstance(document, dict) or not isinstance(document.get("openapi"), str):
        raise ValueError(f"OpenAPI source is not a document: {source_path}")
    return document


def _git(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ValueError(f"Cannot run git for checkout: {checkout} ({exc})") from exc


def _resolve_core_ref(checkout: Path) -> str:
    """Return the checkout's HEAD commit, refusing anything unreproducible."""
    head = _git(checkout, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise ValueError(
            f"Core checkout is not a git repository with a resolvable HEAD: {checkout}"
        )
    core_ref = head.stdout.strip()
    if not _COMMIT_OID.fullmatch(core_ref):
        raise ValueError(f"Core checkout HEAD is not a commit id: {checkout}")

    status = _git(checkout, "status", "--porcelain")
    if status.returncode != 0:
        raise ValueError(f"Cannot read git status for Core checkout: {checkout}")
    if status.stdout.strip():
        raise ValueError(
            f"Core checkout has uncommitted changes: {checkout}. Commit them, or "
            "pass --allow-dirty to stamp a null ref instead."
        )
    return core_ref


def sync_spec(
    checkout: Path,
    destination: Path = SNAPSHOT_PATH,
    *,
    allow_dirty: bool = False,
) -> None:
    """Copy ``shared/openapi.json``, stamping the UTC sync date and Core commit."""
    checkout_path = checkout.expanduser().resolve()
    source_path = checkout_path / "shared" / "openapi.json"
    document = _read_source(source_path)
    try:
        core_ref: str | None = _resolve_core_ref(checkout_path)
    except ValueError:
        if not allow_dirty:
            raise
        # --allow-dirty keeps an unreproducible sync explicit: the snapshot says
        # outright that no Core commit pins it, rather than defaulting to null.
        core_ref = None

    document["x-solwyn-snapshot-date"] = datetime.now(UTC).date().isoformat()
    document["x-solwyn-core-ref"] = core_ref
    serialized = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_mode = (
        stat.S_IMODE(destination.stat().st_mode) if destination.exists() else 0o644
    )
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
            temporary_path.chmod(destination_mode)
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        temporary_path.replace(destination)
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync the CLI OpenAPI snapshot from a local Core checkout."
    )
    parser.add_argument(
        "checkout",
        type=Path,
        help="Path to a local Core checkout containing shared/openapi.json.",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=SNAPSHOT_PATH,
        help="Snapshot destination (defaults to the CLI package snapshot).",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Sync from a dirty or non-git checkout, recording a null "
            "x-solwyn-core-ref instead of failing."
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        sync_spec(args.checkout, args.destination, allow_dirty=args.allow_dirty)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Synced OpenAPI snapshot to {args.destination}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
