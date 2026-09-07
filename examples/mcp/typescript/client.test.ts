import assert from 'node:assert/strict';
import test from 'node:test';
import { decodeResult, providerGate, safeErrorCode, serverEnvironment } from './client.js';

const envelope = (data: unknown, ok = true, error: unknown = null, schema = 'solwyn.mcp.budget-check.v1') => ({ schema, ok, data, error });

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
