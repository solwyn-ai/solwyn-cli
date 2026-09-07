"""Parent-side bounded transport for the private SDK helper."""

from contextlib import suppress
import subprocess
import sys

import anyio

from solwyn_cli._mcp_doctor_ipc import (
    CLEANUP_SECONDS,
    OPERATION_SECONDS,
    OUTPUT_LIMIT,
    HelperResult,
    PrivateRequest,
    isolated_environment,
)


async def _discard_output(stream: anyio.abc.ByteReceiveStream) -> None:
    """Drain without retaining remote bytes while the cleanup deadline runs."""
    try:
        while True:
            await stream.receive(4096)
            await anyio.sleep(0)
    except anyio.EndOfStream:
        return


async def run_helper(request: PrivateRequest) -> HelperResult:
    """Close the liveness pipe first on cancellation, then grant SDK cleanup time."""
    try:
        payload = request.to_private_bytes()
        process = await anyio.open_process(
            [sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=isolated_environment(),
        )
    except Exception:
        return HelperResult(status="failed")
    result = HelperResult(status="failed")
    try:
        with anyio.fail_after(OPERATION_SECONDS + CLEANUP_SECONDS + 3):
            await process.stdin.send(payload)
            output = bytearray()
            while True:
                try:
                    chunk = await process.stdout.receive(1024)
                except anyio.EndOfStream:
                    break
                output.extend(chunk)
                if len(output) > OUTPUT_LIMIT:
                    raise ValueError("Diagnostic output exceeds its limit.")
            await process.wait()
            if process.returncode == 0:
                result = HelperResult.model_validate_json(output)
    except TimeoutError:
        result = HelperResult(status="timeout")
    except Exception:
        result = HelperResult(status="failed")
    finally:
        # SDK starts its server in a separate session. Killing this helper alone
        # cannot prove server cleanup; pipe EOF lets its public contexts unwind.
        with anyio.CancelScope(shield=True):
            await process.stdin.aclose()
            # A rejected oversized result can fill the OS/asyncio pipe buffer.
            # Keep draining during the bounded grace so it cannot prevent exit.
            async with anyio.create_task_group() as drains:
                drains.start_soon(_discard_output, process.stdout)
                try:
                    with anyio.move_on_after(CLEANUP_SECONDS) as cleanup:
                        await process.wait()
                    if cleanup.cancel_called:
                        result = HelperResult(status="cleanup_failed")
                        with suppress(ProcessLookupError):
                            process.terminate()
                        with anyio.move_on_after(2):
                            await process.wait()
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.kill()
                            await process.wait()
                finally:
                    drains.cancel_scope.cancel()
            await process.aclose()
    return result
