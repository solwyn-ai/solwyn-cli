import assert from 'node:assert/strict';
import test from 'node:test';
import { InMemoryTransport, SdkError, SdkErrorCode } from '@modelcontextprotocol/client';
import { createReadClient, decodeResult, providerGate, safeErrorCode, serverEnvironment, validateReadCatalog } from './client.js';

const envelope = (data: unknown, ok = true, error: unknown = null, schema = 'solwyn.mcp.budget-check.v1') => ({ schema, ok, data, error });

// Public read inventory, independently fixed here to catch a stale client gate.
const readNames = [
  'budget_status', 'get_project_status', 'get_forecast', 'get_costs',
  'list_projects', 'get_cost_total', 'list_budget_rules', 'list_tags',
  'list_tag_values', 'list_agent_runs', 'get_agent_run', 'list_alert_events',
  'get_overview', 'list_cost_events', 'get_cost_baseline', 'get_unit_economics',
  'get_cost_drift', 'list_priced_models', 'simulate_costs',
  'list_provider_failovers', 'list_untracked_surfaces', 'list_audit_events',
  'plan_status', 'plan_catalog', 'get_whoami', 'get_account_phone',
  'list_alert_activity', 'list_open_alert_deliveries', 'budget_uncounted',
  'get_account_alert',
];
const readCatalog = () => ({ tools: readNames.map(name => ({ name, annotations: { readOnlyHint: true } })) });

test('complete expanded read inventory permits discovery regardless of ordering', () => {
  validateReadCatalog(readCatalog());
  validateReadCatalog({ tools: readCatalog().tools.reverse() });
});

test('incomplete, duplicate, extra and write-bearing inventories fail closed', () => {
  const tools = readCatalog().tools;
  for (const invalid of [
    { tools: tools.slice(0, 13) },
    { tools: tools.filter(tool => tool.name !== 'budget_status') },
    { tools: tools.filter(tool => tool.name !== 'plan_catalog') },
    { tools: [...tools, { name: 'unknown_read', annotations: { readOnlyHint: true } }] },
    { tools: [...tools.slice(0, -1), tools[0]] },
    { tools: [...tools.slice(0, -1), { name: 'budget_check', annotations: { readOnlyHint: true } }] },
    { tools: [...tools.slice(0, -1), { name: 'get_account_alert', annotations: { readOnlyHint: false } }] },
  ]) assert.throws(() => validateReadCatalog(invalid), /Unexpected read catalog/);
});

test('malformed annotations, catalog shapes and continuations cannot pass discovery', () => {
  const tools = readCatalog().tools;
  for (const invalid of [
    null, {}, { tools: null }, { tools: [null, ...tools.slice(1)] },
    { tools: [...tools.slice(0, -1), { name: 'get_account_alert' }] },
    { tools: [...tools.slice(0, -1), { name: 'get_account_alert', annotations: {} }] },
    { tools: [...tools.slice(0, -1), { name: 'get_account_alert', annotations: { readOnlyHint: 'true' } }] },
    { tools, nextCursor: 'PRIVATE_CURSOR_CANARY' },
  ]) assert.throws(() => validateReadCatalog(invalid), error => {
    assert(error instanceof Error);
    assert.equal(error.message, 'Unexpected read catalog');
    return true;
  });
});

for (const [name, replyDelay, timeout, succeeds] of [
  ['allows discovery after a slow startup', 2_500, 6_000, true],
  ['honors a shorter caller deadline without replay', 250, 50, false],
] as const) {
  test(`modern client ${name}`, async (t) => {
    const client = createReadClient('modern');
    const [transport, server] = InMemoryTransport.createLinkedPair();
    const methods: string[] = [];
    let reply: ReturnType<typeof setTimeout> | undefined;
    t.after(async () => {
      clearTimeout(reply);
      await client.close();
      await server.close();
    });
    server.onmessage = message => {
      assert('method' in message && 'id' in message);
      methods.push(message.method);
      reply = setTimeout(() => {
        void server.send({ jsonrpc: '2.0', id: message.id, result: {
          supportedVersions: ['2026-07-28'], capabilities: {},
        } });
      }, replyDelay);
    };
    await server.start();
    if (succeeds) {
      await client.connect(transport, { timeout });
      assert.equal(client.getProtocolEra(), 'modern');
    } else {
      await assert.rejects(client.connect(transport, { timeout }),
        error => error instanceof SdkError && error.code === SdkErrorCode.RequestTimeout);
    }
    assert.deepEqual(methods, ['server/discover']);
  });
}

test('valid denial is normal, fail-open skips confirm, reserved remains distinct', () => {
  for (const [allowed, reservation, expected] of [
    [false, null, 'denied'], [true, null, 'unreserved'], [true, 'res_synthetic', 'reserved']
  ] as const) {
    assert.equal(providerGate(decodeResult(false, envelope({ allowed, reservation_id: reservation }))), expected);
  }
});

test('malformed decisions, read results and errors cannot open the provider gate', () => {
  for (const data of [{ allowed: 'false', reservation_id: null }, { allowed: true }]) {
    assert.throws(() => providerGate(decodeResult(false, envelope(data))));
  }
  assert.throws(() => providerGate(decodeResult(false, envelope({}, true, null, 'solwyn.mcp.budget-status.v1'))));
  assert.throws(() => providerGate(decodeResult(true, envelope(null, false, { code: 'internal_error' }))));
});

test('inconsistent wire flags fail closed; unknown error text is not printed', () => {
  assert.throws(() => decodeResult(true, envelope({ allowed: true })));
  assert.throws(() => decodeResult(false, envelope(null, false, { code: 'internal_error' })));
  assert.throws(() => decodeResult(false, null));
  assert.equal(safeErrorCode(decodeResult(true, envelope(null, false, { code: 'PRIVATE_CANARY', message: 'PRIVATE_CANARY' }))), 'execution_error');
});

test('contradictory or stopped decisions cannot open the provider gate', () => {
  for (const data of [
    { allowed: false, reservation_id: 'res_synthetic' },
    { allowed: true, reservation_id: '' },
    { allowed: true, reservation_id: 'res_synthetic', run_control: { action: 'terminate' } }
  ]) assert.throws(() => providerGate(decodeResult(false, envelope(data))));
});

test('explicit environment excludes unrelated secrets and preserves intended empty key presence', () => {
  process.env.SOLWYN_API_KEY = '';
  for (const key of ['OPENAI_API_KEY', 'SOLWYN_TAGS', 'OTEL_EXPORTER_OTLP_HEADERS', 'PYTHONPATH', 'NODE_OPTIONS', 'HTTP_PROXY']) {
    process.env[key] = 'PRIVATE_CANARY';
  }
  const environment = serverEnvironment();
  assert.equal(environment.SOLWYN_API_KEY, '');
  assert(!Object.values(environment).includes('PRIVATE_CANARY'));
});

for (const reservation of [null, 'res_existing']) {
  for (const [index, indicator] of [
    { denied_by_period: 'run_stopped' }, { denied_by_period: 'monthly' },
    { denied_by_period: '' }, { reason: 'run_terminated' },
    { denied_by_period: { private: 'PRIVATE_PERIOD_CANARY' } },
    { reason: 'PRIVATE_REASON_CANARY' }
  ].entries()) {
    test(`denial/termination indicator ${index} rejects ${reservation === null ? 'unreserved' : 'reserved'} success`, () => {
      assert.throws(() => providerGate(decodeResult(false, envelope({ allowed: true, reservation_id: reservation, ...indicator }))), (error: unknown) => {
        assert(error instanceof Error);
        assert(!error.message.includes('PRIVATE_'));
        return true;
      });
    });
  }
}

test('omitted/null indicators preserve valid denial, reserved and unreserved decisions', () => {
  for (const indicators of [{}, { denied_by_period: null }, { reason: null }, { denied_by_period: null, reason: null, run_control: null }]) {
    for (const [allowed, reservation, expected] of [[false, null, 'denied'], [true, null, 'unreserved'], [true, 'res_existing', 'reserved']] as const) {
      assert.equal(providerGate(decodeResult(false, envelope({ allowed, reservation_id: reservation, ...indicators }))), expected);
    }
  }
  assert.equal(providerGate(decodeResult(false, envelope({ allowed: false, reservation_id: null, denied_by_period: 'run_stopped', reason: 'run_terminated', run_control: { action: 'terminate' } }))), 'denied');
});
