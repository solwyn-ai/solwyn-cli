"""Fresh-process ambient exporter positive control for diagnostics."""

import json
from pathlib import Path
import sys

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from opentelemetry import baggage, context, propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from solwyn_cli._mcp_doctor_ipc import PrivateRequest
from solwyn_cli._mcp_doctor_process import run_helper
from solwyn_cli._mcp_launch import launch_descriptor

CANARY = "PRIVATE_DIAGNOSTIC_CANARY"
request = PrivateRequest.model_validate_json(Path(sys.argv[1]).read_bytes())
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
propagator = propagate.get_global_textmap()
tracer = trace.get_tracer("synthetic-host")


async def exercise():
    descriptor = launch_descriptor(
        request.command, request.project, request.api_url, request.profile
    )
    with tracer.start_as_current_span("positive-host"):
        token = context.attach(baggage.set_baggage("synthetic", CANARY))
        try:
            with open(Path(sys.argv[1]).with_name("control.stderr"), "w") as errlog:
                async with Client(
                    stdio_client(
                        StdioServerParameters(
                            command=descriptor.command,
                            args=descriptor.args,
                            env={"SOLWYN_API_KEY": request.api_key.get_secret_value()},
                        ),
                        errlog=errlog,
                    ),
                    mode="auto",
                    cache=None,
                ) as client:
                    await client.list_tools()
                    await client.call_tool(
                        "budget_status", {"project_id": request.project}
                    )
            positive_count = len(exporter.get_finished_spans())
            assert positive_count >= 3
            isolated = await run_helper(request)
            assert isolated.status == "ok" and isolated.read == "ok"
            assert len(exporter.get_finished_spans()) == positive_count
        finally:
            context.detach(token)
    with tracer.start_as_current_span("after-host"):
        pass
    assert trace.get_tracer_provider() is provider
    assert propagate.get_global_textmap() is propagator
    spans = exporter.get_finished_spans()
    assert any(span.name == "after-host" for span in spans)
    assert CANARY not in json.dumps([span.to_json() for span in spans])
    print(
        json.dumps(
            {
                "positive_sdk_spans": positive_count,
                "helper_sdk_spans": 0,
                "parent_provider_unchanged": True,
                "parent_propagator_unchanged": True,
            }
        )
    )


anyio.run(exercise)
