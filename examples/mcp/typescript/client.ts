/** Read-first Solwyn client. See docs/mcp-quickstart.md; no provider calls. */
import { statSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { Client, type CallToolResult } from '@modelcontextprotocol/client';
import { getDefaultEnvironment, StdioClientTransport } from '@modelcontextprotocol/client/stdio';

type ObjectValue = Record<string, unknown>;
export type Envelope =
  | { schema: string; ok: true; data: ObjectValue; error: null }
  | { schema: string; ok: false; data: ObjectValue | null; error: { code: string } };
const projectPattern = /^proj_[a-f0-9]{24}$/;
const safeCodes = new Set(['authentication_required', 'authentication_failed', 'invalid_arguments', 'forbidden', 'read_only_key', 'jwt_required']);

function object(value: unknown): value is ObjectValue {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function decodeResult(isError: boolean, payload: unknown): Envelope {
  if (!object(payload) || typeof payload.schema !== 'string' || typeof payload.ok !== 'boolean' || isError === payload.ok) {
    throw new Error('Invalid tool result');
  }
  if (payload.ok && object(payload.data) && payload.error === null) {
    return { schema: payload.schema, ok: true, data: payload.data, error: null };
  }
  if (!payload.ok && (payload.data === null || object(payload.data)) && object(payload.error) && typeof payload.error.code === 'string') {
    return { schema: payload.schema, ok: false, data: payload.data, error: { code: payload.error.code } };
  }
  throw new Error('Inconsistent tool result');
}

/** Interpret a fresh decision only; the caller still owns expiry and run state. */
export function providerGate(result: Envelope): 'denied' | 'unreserved' | 'reserved' {
  if (!result.ok || result.schema !== 'solwyn.mcp.budget-check.v1') throw new Error('No budget decision');
  const { allowed, reservation_id: reservation, denied_by_period: period, reason } = result.data;
  if (typeof allowed !== 'boolean' || !(reservation === null || (typeof reservation === 'string' && reservation.length > 0)) || (period != null && typeof period !== 'string') || (reason != null && reason !== 'run_terminated')) {
    throw new Error('Invalid budget decision');
  }
  if ((!allowed && reservation !== null) || (allowed && (result.data.run_control != null || period != null || reason != null))) throw new Error('Contradictory budget decision');
  if (!allowed) return 'denied';
  return reservation === null ? 'unreserved' : 'reserved';
}

export function safeErrorCode(result: Envelope): string {
  return !result.ok && safeCodes.has(result.error.code) ? result.error.code : 'execution_error';
}

export function serverEnvironment(): Record<string, string> {
  // The SDK also merges this documented OS baseline. Never spread process.env.
  const environment = getDefaultEnvironment();
  if (process.env.SOLWYN_API_KEY !== undefined) environment.SOLWYN_API_KEY = process.env.SOLWYN_API_KEY;
  return environment;
}

async function closeAndObserve(client: Client, transport: StdioClientTransport): Promise<void> {
  const pid = transport.pid; // Public SDK API, captured before close clears it.
  await client.close();
  await transport.close(); // Also covers connection failure before client ownership.
  if (pid === null) return;
  const until = Date.now() + 5_000;
  while (true) {
    try { process.kill(pid, 0); }
    catch (error) {
      if (object(error) && error.code === 'ESRCH') return;
      throw new Error('Cannot verify shutdown');
    }
    if (Date.now() >= until) throw new Error('Server shutdown not observed');
    await delay(20);
  }
}

async function main(): Promise<number> {
  const { values } = parseArgs({ options: {
    executable: { type: 'string' }, project: { type: 'string' },
    'api-url': { type: 'string', default: 'https://api.solwyn.ai' },
    mode: { type: 'string', default: 'modern' },
    timeout: { type: 'string', default: '10' },
    'validation-error': { type: 'boolean', default: false },
  } });
  const executable = values.executable;
  const project = values.project;
  const timeout = Number(values.timeout) * 1_000;
  const url = new URL(values['api-url']!);
  if (!executable || !isAbsolute(executable) || !statSync(executable).isFile() || !project || !projectPattern.test(project)) throw new Error('Invalid launch');
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Invalid API URL');
  if (!['modern', 'legacy'].includes(values.mode!) || !Number.isFinite(timeout) || timeout < 100 || timeout > 30_000) throw new Error('Invalid client options');
  const client = new Client({ name: 'solwyn-read-example', version: '0.0.0' }, {
    // SDK2 defaults to legacy. Pin modern explicitly and bound its disposable
    // sibling discovery probe. Startup must have no spend/provider effects.
    versionNegotiation: { mode: values.mode === 'legacy' ? 'legacy' : { pin: '2026-07-28' }, probe: { timeoutMs: 2_000, maxRetries: 0 } },
  });
  const transport = new StdioClientTransport({
    command: executable,
    args: ['--project', project, '--api-url', values['api-url']!, 'mcp', '--profile', 'read'],
    env: serverEnvironment(), stderr: 'pipe', maxBufferSize: 2 * 1024 * 1024,
  });
  transport.stderr?.on('data', () => {}); // Drain; never reflect arbitrary SDK/server text.
  const abort = new AbortController();
  const cancel = () => abort.abort();
  const timer = setTimeout(cancel, timeout);
  process.once('SIGINT', cancel);
  process.once('SIGTERM', cancel);
  let output: ObjectValue;
  let code = 0;
  try {
    const options = { timeout, signal: abort.signal };
    await client.connect(transport, options);
    if (client.getProtocolEra() !== (values.mode === 'legacy' ? 'legacy' : 'modern')) throw new Error('Unexpected protocol');
    const catalog = await client.listTools({}, { ...options, cacheMode: 'refresh' });
    if (catalog.tools.length !== 13 || !catalog.tools.every(tool => tool.annotations?.readOnlyHint) || !catalog.tools.some(tool => tool.name === 'budget_status')) throw new Error('Unexpected read catalog');
    const wire: CallToolResult = await client.callTool({ name: 'budget_status', arguments: values['validation-error'] ? { projectid: 'PRIVATE_VALIDATION_CANARY' } : {} }, options);
    const result = decodeResult(wire.isError === true, wire.structuredContent);
    const protocol = values.mode === 'legacy' ? '2025-11-25' : '2026-07-28';
    if (!result.ok) {
      output = { status: 'execution_error', code: safeErrorCode(result), protocol };
      code = 1;
    } else {
      const data = result.data;
      if (result.schema !== 'solwyn.mcp.budget-status.v1' || data.project_id !== project || typeof data.remaining_budget !== 'number' || !Number.isFinite(data.remaining_budget)) throw new Error('Invalid budget read');
      output = { status: 'ok', protocol, tools: catalog.tools.length, project_id: data.project_id, remaining_budget: data.remaining_budget };
    }
  } finally {
    clearTimeout(timer);
    process.removeListener('SIGINT', cancel);
    process.removeListener('SIGTERM', cancel);
    // SDK close ends stdin, then terminates/kills the direct child. Cleanup is
    // additional to useful-work time. No arbitrary-descendant guarantee.
    await closeAndObserve(client, transport);
  }
  console.log(JSON.stringify(output));
  return code;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().then(code => { process.exitCode = code; }).catch(() => {
    console.log(JSON.stringify({ status: 'client_error', code: 'connection_or_read_failed' }));
    process.exitCode = 1;
  });
}
