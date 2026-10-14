"""Shared synchronous attribution transport for CLI and MCP."""

import math
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import typer
from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli._vendor.wire import CALL_ID_PATTERN, IngestMetadataEvent, TokenDetails
from solwyn_cli.client import SolwynClient
from solwyn_cli import _mcp_operation as operation
from solwyn_cli.commands._tags import validate_tags
from solwyn_cli.errors import ApiError, INGEST_BUSY_HINT

CALL_ID_HINT = "Use a canonical lowercase UUID: uuidgen | tr A-F a-f"
_REJECTION_HINTS = {
    "unknown_model": "Use the exact model name.",
    "unknown_service_tier": "Use a supported service tier for this model.",
    "invalid_tags": "Check tag keys, values, and documented bounds.",
    "tag_cardinality_exceeded": "Reduce the number of distinct tag values.",
    "unsupported_modality": "Use a supported modality for this model.",
}


class IngestRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    code: Literal[
        "unknown_model",
        "unknown_service_tier",
        "invalid_tags",
        "tag_cardinality_exceeded",
        "unsupported_modality",
    ]
    model: str
    message: str


class IngestDuplicate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    reason: Literal["call_id", "legacy_key"]
    call_id: str


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ingested: int = Field(ge=0)
    rejected: list[IngestRejection] = Field(default_factory=list)
    duplicates: list[IngestDuplicate] = Field(default_factory=list)


class RecordingResult(IngestResponse):
    status: Literal[
        "ingested",
        "deduplicated",
        "rejected",
        "skipped",
        "failed",
        "stopped",
        "unknown",
        "not_recorded",
    ]
    call_ids: list[str] = Field(default_factory=list)


def validate_call_id(call_id: str) -> str:
    if re.fullmatch(CALL_ID_PATTERN, call_id) is None:
        raise ValueError(CALL_ID_HINT)
    return call_id


def build_event(**inputs: object) -> IngestMetadataEvent:
    """Build one event; caller may share a fresh instance id across a batch."""
    values: dict[str, object] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0.0,
        "status": "success",
        "is_model_fallback": False,
        "sdk_instance_id": str(uuid4()),
        "timestamp": datetime.now(UTC),
        "call_id": str(uuid4()),
    }
    values.update(inputs)
    if inputs.get("token_details") is not None:
        details = TokenDetails.model_validate(inputs["token_details"])
        for name in ("input_tokens", "output_tokens"):
            count = getattr(details, name)
            if name in inputs and inputs[name] != count:
                raise ValueError("Top-level token counts must match token_details.")
            values[name] = count
    validate_call_id(values["call_id"])
    event = IngestMetadataEvent.model_validate(values)
    validate_tags(event.tags)
    if not math.isfinite(event.latency_ms) or event.latency_ms < 0:
        raise ValueError("Latency must be a finite nonnegative number.")
    return event


def request_ingest(
    *, client: SolwynClient, events: list[IngestMetadataEvent]
) -> RecordingResult:
    """Post one batch through the client's bounded capacity retry path."""
    operation.checkpoint()
    if len(events) == 1:
        operation.remember(call_id=events[0].call_id, recording="unknown")
    try:
        response = client.request(
            "POST",
            "/api/v1/metadata/ingest",
            json=[event.model_dump(mode="json", exclude_none=True) for event in events],
        )
    except ApiError as error:
        if error.status_code == 503:
            operation.remember(recording="not_recorded")
        raise
    if response.status_code != 202:
        raise ValueError("Metadata ingest did not return 202.")
    data = IngestResponse.model_validate(response.json())
    indexes = [item.index for item in [*data.rejected, *data.duplicates]]
    if len(set(indexes)) != len(indexes) or any(
        index >= len(events) for index in indexes
    ):
        raise ValueError("Invalid ingest disposition indexes.")
    if data.ingested + len(indexes) != len(events):
        raise ValueError(
            "Incomplete ingest dispositions; durability could not be verified."
        )
    for duplicate in data.duplicates:
        if duplicate.call_id != events[duplicate.index].call_id:
            raise ValueError(
                "Ingest duplicate call ID did not match the submitted event."
            )
    data.rejected = [
        rejection.model_copy(
            update={
                "model": events[rejection.index].model,
                "message": _REJECTION_HINTS[rejection.code],
            }
        )
        for rejection in data.rejected
    ]
    for rejection in data.rejected:
        typer.echo(
            f"Recording rejected ({rejection.code}). {rejection.message}", err=True
        )
    legacy = any(item.reason == "legacy_key" for item in data.duplicates)
    if legacy:
        typer.echo(
            "Warning: legacy_key collision cannot prove this logical call is durable.",
            err=True,
        )
    status = (
        ("unknown" if operation.current() else "failed")
        if legacy
        else "rejected"
        if data.rejected
        else "deduplicated"
        if data.duplicates
        else "ingested"
    )
    operation.remember(recording=status)
    return RecordingResult(
        **data.model_dump(), status=status, call_ids=[event.call_id for event in events]
    )


def record_best_effort(
    *, client: SolwynClient, event: IngestMetadataEvent, no_record: bool = False
) -> RecordingResult:
    """Recording must never undo successful settlement or the denial decision."""
    if no_record:
        operation.remember(recording="skipped")
        return RecordingResult(status="skipped", ingested=0, call_ids=[event.call_id])
    try:
        result = request_ingest(client=client, events=[event])
        if result.status in ("rejected", "failed", "unknown"):
            typer.echo(
                "Warning: attribution acknowledgement is unknown; the budget outcome is unchanged."
                if result.status == "unknown"
                else "Warning: attribution was not recorded; the budget outcome is unchanged.",
                err=True,
            )
        operation.remember(recording=result.status)
        return result
    except operation.OperationLimit:
        raise
    except ApiError as error:
        if error.status_code == 503:
            typer.echo(f"Warning: {INGEST_BUSY_HINT}", err=True)
            return RecordingResult(
                status="not_recorded", ingested=0, call_ids=[event.call_id]
            )
        typer.echo(
            "Warning: attribution recording failed; preserve the call identity for recovery.",
            err=True,
        )
        return RecordingResult(
            status="unknown" if operation.current() else "failed",
            ingested=0,
            call_ids=[event.call_id],
        )
    except Exception:
        # Settlement/denial already succeeded. Preserve that known state even on
        # an unexpected recording failure, without printing its private details.
        typer.echo(
            "Warning: attribution recording failed; preserve the call identity for recovery.",
            err=True,
        )
        return RecordingResult(
            status="unknown" if operation.current() else "failed",
            ingested=0,
            call_ids=[event.call_id],
        )
