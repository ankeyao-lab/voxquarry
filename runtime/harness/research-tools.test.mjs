import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { apply } from './research-tools.mjs';

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'wenquan-tool-test-'));
  const catalogPath = join(directory, 'catalog.json');
  const tokenPath = join(directory, 'capability');
  writeFileSync(catalogPath, JSON.stringify(['submit_answer', 'read_report'].map(name => ({
    name, description: 'Synthetic test only', parameters: { type: 'object' }, concurrency_safe: name === 'read_report',
  }))));
  writeFileSync(tokenPath, 'synthetic-only-tool-capability');
  const registered = new Map();
  apply({ tools: { register(tool) { registered.set(tool.name, tool); } } }, {
    catalogPath, tokenPath, brokerUrl: 'http://relay:9000/broker/',
  });
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; rmSync(directory, { recursive: true }); });
  let conclusions = 0;
  const execution = { callId: 'synthetic-call', concludeTurn() { conclusions++; } };
  return { registered, execution, conclusions: () => conclusions };
}

test('accepted answer concludes the native turn and preserves the broker result', async t => {
  const f = fixture(t);
  const body = JSON.stringify({ accepted: true, research_progress: { remaining_model_calls: 7 } });
  globalThis.fetch = async (url, options) => {
    assert.equal(String(url), 'http://relay:9000/broker/tools/submit_answer');
    assert.equal(JSON.parse(options.body).tool_call_id, 'synthetic-call');
    return new Response(body);
  };
  assert.equal(await f.registered.get('submit_answer').execute({ answer: 'Saved draft' }, f.execution), body);
  assert.equal(f.conclusions(), 1);
});

test('only an explicit trusted catalog annotation enables native parallel scheduling', t => {
  const f = fixture(t);
  assert.equal(f.registered.get('read_report').isConcurrencySafe({}), true);
  assert.equal(f.registered.get('submit_answer').isConcurrencySafe({}), false);
});

test('rejected or malformed acceptance stays available for correction', async t => {
  const f = fixture(t);
  for (const result of [{ accepted: false }, { accepted: 'true' }, {}, null]) {
    const body = JSON.stringify(result);
    globalThis.fetch = async () => new Response(body);
    assert.equal(await f.registered.get('submit_answer').execute({}, f.execution), body);
  }
  assert.equal(f.conclusions(), 0);
});

test('business errors become native errors while preserving structured recovery', async t => {
  const f = fixture(t);
  const error = {code: 'dataset_too_large', origin: 'capacity', retryable: false,
    diagnostic: {reason: 'source_metadata_bytes', observed: 33563529, limit: 33554432},
    recovery_action: 'continue_original_scope_batches'};
  globalThis.fetch = async () => new Response(JSON.stringify({operation_success: false, error}));
  await assert.rejects(f.registered.get('submit_answer').execute({}, f.execution), failure => {
    assert.deepEqual(JSON.parse(failure.message), {operation_success: false, error});
    return true;
  });
  assert.equal(f.conclusions(), 0);
});

test('other tools never end the turn even when their business result says accepted', async t => {
  const f = fixture(t);
  globalThis.fetch = async () => new Response('{"accepted":true}');
  await f.registered.get('read_report').execute({}, f.execution);
  assert.equal(f.conclusions(), 0);
});

test('transport and JSON failures cannot conclude the turn', async t => {
  const f = fixture(t);
  for (const response of [new Response('{"accepted":true}', { status: 503 }), new Response('{broken')]) {
    globalThis.fetch = async () => response;
    await assert.rejects(f.registered.get('submit_answer').execute({}, f.execution));
  }
  assert.equal(f.conclusions(), 0);
});
