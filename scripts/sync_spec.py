"""Copy the customer OpenAPI contract from a local Core checkout."""

import argparse
import json
import sys
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


def sync_spec(checkout: Path, destination: Path = SNAPSHOT_PATH) -> None:
    """Copy ``shared/openapi.json`` and add the current UTC sync date."""
    source_path = checkout.expanduser().resolve() / "shared" / "openapi.json"
    document = _read_source(source_path)
    document["x-solwyn-snapshot-date"] = datetime.now(UTC).date().isoformat()

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


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
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        sync_spec(args.checkout, args.destination)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Synced OpenAPI snapshot to {args.destination}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
