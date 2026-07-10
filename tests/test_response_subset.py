"""Guard every response field currently consumed by the CLI."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest


SNAPSHOT_PATH = Path("src/solwyn_cli/spec/openapi_snapshot.json")
# P3+ command tasks extend this registry when they begin reading response fields.
RESPONSE_READS = {
    "TokenResponse": ("access_token", "refresh_token", "token_type"),
    "Error402Response": (
        "detail.code",
        "detail.message",
        "detail.cta_path",
    ),
    "UpgradeRequiredDetail": ("code", "message", "cta_path"),
    "QuotaExceededDetail": ("code", "message", "cta_path"),
    "ProjectLimitReachedDetail": ("code", "message", "cta_path"),
    # Generic HTTP error parsing consumes detail; retry timing uses the header.
    "RateLimitErrorResponse": ("detail",),
}


def _assert_field_path(
    schema: dict[str, Any],
    document: dict[str, Any],
    segments: tuple[str, ...],
    registered_path: str,
    context: str,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        component = reference.rsplit("/", maxsplit=1)[-1]
        target = document["components"]["schemas"][component]
        _assert_field_path(target, document, segments, registered_path, component)
        return

    union = schema.get("oneOf") or schema.get("anyOf")
    if union is not None:
        branches = [branch for branch in union if branch.get("type") != "null"]
        for branch in branches:
            _assert_field_path(branch, document, segments, registered_path, context)
        return

    if not segments:
        return
    field, *remaining = segments
    properties = schema.get("properties", {})
    if field not in properties:
        raise AssertionError(
            f"{registered_path}: consumed field '{field}' is missing in {context}"
        )
    _assert_field_path(
        properties[field],
        document,
        tuple(remaining),
        registered_path,
        context,
    )


def assert_response_reads_exist(snapshot: dict[str, Any]) -> None:
    """Validate the explicit response-read registry against the snapshot."""
    schemas = snapshot["components"]["schemas"]
    for component, paths in RESPONSE_READS.items():
        assert component in schemas, f"Missing response component: {component}"
        for path in paths:
            registered_path = f"{component}.{path}"
            _assert_field_path(
                schemas[component],
                snapshot,
                tuple(path.split(".")),
                registered_path,
                component,
            )


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_every_current_response_read_exists_in_snapshot() -> None:
    assert_response_reads_exist(_snapshot())


def test_removed_consumed_union_field_trips_response_subset_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    schemas = mutated["components"]["schemas"]
    schemas["QuotaExceededDetail"]["properties"].pop("message")

    with pytest.raises(
        AssertionError,
        match=r"Error402Response\.detail\.message.*QuotaExceededDetail",
    ):
        assert_response_reads_exist(mutated)
