"""Record caller-owned spend without a budget reservation."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from pydantic import ValidationError

from solwyn_cli._vendor.wire import (
    MediaUsage,
    Modality,
    ProviderName,
    ServiceTier,
    TokenDetails,
)
from solwyn_cli.auth import AuthError, JwtCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._record import (
    CALL_ID_HINT,
    RecordingResult,
    build_event,
    request_ingest,
)
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands._tags import parse_tags
from solwyn_cli.errors import ApiError, INGEST_UNKNOWN_HINT, is_ingest_unaccepted
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

RECORD_HELP = help_contract(
    endpoint="POST /api/v1/metadata/ingest",
    auth="key",
    exits="0 recorded or call-ID replay; 1 rejection/collision/API; 2 usage; 3 auth",
    schema="solwyn.record.v1",
    guidance=(
        "Record only caller-made spend. Costs are computed server-side; local providers are accepted at $0.",
        "Batch accepts an array of event inputs, chunks 1000 per request, and uses a fresh invocation ID. Do not supply cost_usd or lease_id.",
        "Reuse call_id when retrying the same logical event. Use SOLWYN_AGENT_RUN_ID to group a job.",
    ),
)


def recording_table(data: RecordingResult) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Status", data.status),
            ("Ingested", data.ingested),
            ("Rejected", len(data.rejected)),
            ("Duplicates", len(data.duplicates)),
            ("Call IDs", ", ".join(data.call_ids)),
        ),
    )


def record(
    context: typer.Context,
    model: Annotated[str | None, typer.Option("--model")] = None,
    provider: Annotated[ProviderName | None, typer.Option("--provider")] = None,
    call_id: Annotated[
        str | None,
        typer.Option(
            "--call-id", help="Canonical lowercase UUID; generated if omitted."
        ),
    ] = None,
    batch: Annotated[
        str | None,
        typer.Option(
            "--batch", help="JSON array file or - for stdin; chunks at 1000 events."
        ),
    ] = None,
    latency_ms: Annotated[float, typer.Option("--latency-ms", min=0)] = 0,
    status: Annotated[
        str, typer.Option("--status", help="success or error.")
    ] = "success",
    agent_run_id: Annotated[
        str | None, typer.Option("--agent-run-id", envvar="SOLWYN_AGENT_RUN_ID")
    ] = None,
    parent_run_id: Annotated[str | None, typer.Option("--parent-run-id")] = None,
    run_name: Annotated[str | None, typer.Option("--run-name")] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag", help="Verbatim key=value; repeatable; overrides SOLWYN_TAGS."
        ),
    ] = None,
    provider_region: Annotated[str | None, typer.Option("--provider-region")] = None,
    service_tier: Annotated[ServiceTier | None, typer.Option("--service-tier")] = None,
    modality: Annotated[Modality, typer.Option("--modality")] = Modality.TEXT,
    image_count: Annotated[int | None, typer.Option("--image-count", min=0)] = None,
    generation_count: Annotated[
        int | None, typer.Option("--generation-count", min=0)
    ] = None,
    video_seconds: Annotated[
        float | None, typer.Option("--video-seconds", min=0)
    ] = None,
    audio_seconds: Annotated[
        float | None, typer.Option("--audio-seconds", min=0)
    ] = None,
    input_characters: Annotated[
        int | None, typer.Option("--input-characters", min=0)
    ] = None,
    resolution: Annotated[str | None, typer.Option("--resolution")] = None,
    quality: Annotated[str | None, typer.Option("--quality")] = None,
    input_tokens: Annotated[
        int,
        typer.Option("--input-tokens", min=0, max=100_000_000),
    ] = 0,
    output_tokens: Annotated[
        int,
        typer.Option("--output-tokens", min=0, max=100_000_000),
    ] = 0,
    cached_input_tokens: Annotated[
        int,
        typer.Option("--cached-input-tokens", min=0, max=100_000_000),
    ] = 0,
    cache_creation_5m_tokens: Annotated[
        int,
        typer.Option("--cache-creation-5m-tokens", min=0, max=100_000_000),
    ] = 0,
    cache_creation_1h_tokens: Annotated[
        int,
        typer.Option("--cache-creation-1h-tokens", min=0, max=100_000_000),
    ] = 0,
    reasoning_tokens: Annotated[
        int,
        typer.Option("--reasoning-tokens", min=0, max=100_000_000),
    ] = 0,
    audio_input_tokens: Annotated[
        int,
        typer.Option("--audio-input-tokens", min=0, max=100_000_000),
    ] = 0,
    audio_output_tokens: Annotated[
        int,
        typer.Option("--audio-output-tokens", min=0, max=100_000_000),
    ] = 0,
    image_input_tokens: Annotated[
        int,
        typer.Option(
            "--image-input-tokens",
            min=0,
            max=100_000_000,
            help="Image input tokens, already counted within --input-tokens.",
        ),
    ] = 0,
    image_output_tokens: Annotated[
        int,
        typer.Option(
            "--image-output-tokens",
            min=0,
            max=100_000_000,
            help="Image output tokens, already counted within --output-tokens.",
        ),
    ] = 0,
    accepted_prediction_tokens: Annotated[
        int,
        typer.Option("--accepted-prediction-tokens", min=0, max=100_000_000),
    ] = 0,
    rejected_prediction_tokens: Annotated[
        int,
        typer.Option("--rejected-prediction-tokens", min=0, max=100_000_000),
    ] = 0,
    tool_use_input_tokens: Annotated[
        int,
        typer.Option("--tool-use-input-tokens", min=0, max=100_000_000),
    ] = 0,
    is_estimated: Annotated[
        bool,
        typer.Option(
            "--is-estimated",
            help="Mark the token breakdown as estimated rather than provider-reported.",
        ),
    ] = False,
) -> None:
    """Record one completed call, or a batch, without reserving budget."""
    try:
        tags = parse_tags(tag)
        sdk_instance_id = str(uuid4())
        if batch is not None:
            # Per-event fields belong in the array, preventing silently ignored flags.
            incompatible = [
                name
                for name in context.params
                if name != "batch"
                and context.get_parameter_source(name).name == "COMMANDLINE"
            ]
            if incompatible:
                raise ValueError(
                    "With --batch, put event inputs in the JSON array instead of flags."
                )
            raw = json.loads(
                sys.stdin.read() if batch == "-" else Path(batch).read_text()
            )
            if not isinstance(raw, list) or not raw:
                raise ValueError("Batch must be a nonempty JSON array.")
            start = datetime.now(UTC)
            events = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise ValueError("Each batch event must be a JSON object.")
                defaults = dict(
                    sdk_instance_id=sdk_instance_id,
                    timestamp=start + timedelta(microseconds=index),
                    agent_run_id=agent_run_id,
                    tags=tags,
                )
                defaults.update(item)
                # The emitting process is always this invocation, never a persisted ID.
                defaults["sdk_instance_id"] = sdk_instance_id
                events.append(build_event(**defaults))
        else:
            if model is None or provider is None:
                raise ValueError("Single recording requires --model and --provider.")
            if status not in ("success", "error"):
                raise ValueError("Status must be success or error.")
            tokens = TokenDetails(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_creation_5m_tokens=cache_creation_5m_tokens,
                cache_creation_1h_tokens=cache_creation_1h_tokens,
                reasoning_tokens=reasoning_tokens,
                audio_input_tokens=audio_input_tokens,
                audio_output_tokens=audio_output_tokens,
                image_input_tokens=image_input_tokens,
                image_output_tokens=image_output_tokens,
                accepted_prediction_tokens=accepted_prediction_tokens,
                rejected_prediction_tokens=rejected_prediction_tokens,
                tool_use_input_tokens=tool_use_input_tokens,
                is_estimated=is_estimated,
            )
            quantities = dict(
                image_count=image_count,
                generation_count=generation_count,
                video_seconds=video_seconds,
                audio_seconds=audio_seconds,
                input_characters=input_characters,
                resolution=resolution,
                quality=quality,
            )
            media = (
                MediaUsage(**quantities, is_estimated=is_estimated)
                if any(value is not None for value in quantities.values())
                else None
            )
            inputs = dict(
                model=model,
                provider=provider,
                sdk_instance_id=sdk_instance_id,
                latency_ms=latency_ms,
                status=status,
                agent_run_id=agent_run_id,
                parent_agent_run_id=parent_run_id,
                agent_run_name=run_name,
                tags=tags,
                provider_region=provider_region,
                service_tier=service_tier,
                modality=modality,
                token_details=tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                media_usage=media,
            )
            if call_id is not None:
                inputs["call_id"] = call_id
            events = [build_event(**inputs)]
    except (ValueError, TypeError, OSError) as error:
        exit_with_error(
            context,
            OutputError(
                code="invalid_record_request",
                message="The recording request is invalid.",
                hint=f"{error}. {CALL_ID_HINT}",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="project_key_required",
                message="Recording requires a project API key.",
                hint="Set SOLWYN_API_KEY to a write-scoped project key.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    runtime = command_context(context)
    result = RecordingResult(
        status="ingested", ingested=0, call_ids=[event.call_id for event in events]
    )
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            for offset in range(0, len(events), 1000):
                page = request_ingest(
                    client=client, events=events[offset : offset + 1000]
                )
                result.ingested += page.ingested
                result.rejected.extend(
                    item.model_copy(update={"index": item.index + offset})
                    for item in page.rejected
                )
                result.duplicates.extend(
                    item.model_copy(update={"index": item.index + offset})
                    for item in page.duplicates
                )
                if batch:
                    typer.echo(
                        f"Recorded batch progress: {min(offset + 1000, len(events))}/{len(events)} events processed.",
                        err=True,
                    )
        result.status = (
            "failed"
            if any(item.reason == "legacy_key" for item in result.duplicates)
            else "rejected"
            if result.rejected
            else "deduplicated"
            if result.duplicates
            else "ingested"
        )
    except ApiError as error:
        # Retain all input identities and already-known dispositions after an
        # uncertain/partial write, so retrying the input cannot mint new calls.
        if (
            error.status_code is None
            or error.status_code >= 500
            or error.ingest_outcome_uncertain
            or result.ingested
            or result.rejected
            or result.duplicates
        ):
            result.status = (
                "not_recorded"
                if is_ingest_unaccepted(error)
                and not (result.ingested or result.rejected or result.duplicates)
                else "failed"
            )
            output_engine(context).write_success(
                schema="solwyn.record.v1", data=result, table=recording_table(result)
            )
        exit_with_error(
            context,
            error,
            semantic=ErrorSemantic.USAGE if error.status_code in (400, 422) else None,
        )
    except AuthError as error:
        if error.ingest_outcome_uncertain or (
            result.ingested or result.rejected or result.duplicates
        ):
            result.status = "failed"
            output_engine(context).write_success(
                schema="solwyn.record.v1", data=result, table=recording_table(result)
            )
        exit_with_error(
            context,
            OutputError(
                code="authentication_failed",
                message=str(error),
                hint=INGEST_UNKNOWN_HINT if error.ingest_outcome_uncertain else None,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        result.status = "failed"
        output_engine(context).write_success(
            schema="solwyn.record.v1", data=result, table=recording_table(result)
        )
        exit_with_error(
            context,
            OutputError(
                code="invalid_ingest_response",
                message="The ingest response was invalid.",
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.record.v1", data=result, table=recording_table(result)
    )
    if result.status in ("rejected", "failed"):
        raise typer.Exit(code=1)
