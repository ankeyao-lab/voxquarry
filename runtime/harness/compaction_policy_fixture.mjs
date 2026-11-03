// Offline boundary fixture against the unmodified pinned official plugin.
// check_compaction_policy.py supplies only a temporary module-resolution link.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {Context} from '@deepseek-ai/cordis';
import {BasicCompactionEngine} from '@deepseek-ai/dsh-compaction-basic';

const require = createRequire(import.meta.url);
const version = require('@deepseek-ai/dsh-compaction-basic/package.json').version;
assert.equal(version, '0.1.5-rc.1');
const patch = readFileSync(process.argv[2], 'utf8');
const expression = key => patch.match(new RegExp(`${key}: !!js (.+)`))[1];
const cases = [];
globalThis.fetch = () => { throw new Error('Network access is forbidden in this fixture'); };
for (const [capacity, working, threshold, retained] of [
  [1000000, 200000, 200000, 40000], [200000, 200000, 160000, 32000], [1000000, 20000, 20000, 4000],
]) {
  const env = {DSH_CONTEXT_WINDOW: String(capacity), DSH_RESEARCH_CONTEXT_TOKENS: String(working)};
  // These expressions are executable configuration from our trusted patch.
  const evaluate = key => new Function('process', `return (${expression(key)});`)({env});
  assert.equal(evaluate('defaultContextWindow'), capacity);
  const ctx = new Context();
  let tokens = threshold - 1, triggers = 0;
  ctx.provide('llm', {resolveModelInfo: async () => ({context: {contextWindow: capacity}})});
  ctx.provide('tokenMeter', {measure: () => ({totalTokens: tokens, nodes: []})});
  ctx.provide('toolResultPruner', {pruneSession: () => { triggers++; tokens = 0; }});
  const engine = new BasicCompactionEngine(ctx, {
    thresholdRatio: evaluate('thresholdRatio'), retainRatio: evaluate('retainRatio'),
  });
  assert.equal(engine.config.auto, true);
  assert.equal(engine.config.maxTokens, 8192);
  assert.equal(engine.config.compactionRetries, 1);
  assert.equal(engine.config.maxOverflowRetries, 1);
  assert.equal(Math.floor(capacity * engine.config.retainRatio), retained);
  const agent = {session: {seq: 0, requestHeader: () => ({config: {provider: 'deepseek-official', model: 'synthetic'}})}};
  await engine.compactIfNeeded(agent, 'pressure');
  assert.equal(triggers, 0, 'Below the native threshold, history must not be pruned');
  tokens = threshold;
  await engine.compactIfNeeded(agent, 'pressure');
  assert.equal(triggers, 1, 'The official pressure policy must trigger at the independent threshold');
  cases.push({provider_capacity: capacity, working_tokens: working, threshold_tokens: threshold, retained_tokens: retained});
}
assert.throws(() => new BasicCompactionEngine(new Context(), {thresholdRatio: .2, retainRatio: .2}), /retainRatio/);
console.log(JSON.stringify({status: 'passed', official_plugin: version, real_model_calls: 0, cases,
  scope: 'native_config_and_pressure_gate;synthetic_meter_and_pruner;not_full_summary_or_session_restore'}));
