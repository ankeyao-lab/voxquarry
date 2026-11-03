import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { apply } from './native-web.mjs';

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'wenquan-web-test-'));
  const tokenPath = join(directory, 'capability');
  writeFileSync(tokenPath, 'synthetic-web-capability');
  let search, fetchPage;
  apply({ web: { registerSearchProvider(provider) { search = provider; },
    registerFetchProvider(provider) { fetchPage = provider; } } }, { tokenPath, brokerUrl: 'http://relay:9000/broker/' });
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; rmSync(directory, { recursive: true }); });
  return { search, fetchPage };
}

test('native search and fetch retain saved source handles through the fixed relay', async t => {
  const f = fixture(t);
  globalThis.fetch = async (url, options) => {
    assert.equal(new URL(url).origin, 'http://relay:9000');
    assert.equal(options.headers.Authorization, 'Bearer synthetic-web-capability');
    return new Response(JSON.stringify({ source_id: 'src_fixture', registered_urls: ['https://example.test/manual'], text: 'A manual' }));
  };
  const found = await f.search.search({ query: 'manual' });
  assert.equal(found.sources[0].url, 'https://example.test/manual');
  assert.match(found.answer, /src_fixture/);
  assert.equal(found.answer.includes('synthetic-web-capability'), false);
  const page = await f.fetchPage.fetch({ url: 'https://example.test/manual' });
  assert.equal(page.body.kind, 'text');
  assert.match(page.body.content, /src_fixture/);
});

test('network preference denial is a failure and cannot masquerade as an empty page', async t => {
  const f = fixture(t);
  globalThis.fetch = async () => new Response('{"error":{"code":"web_disabled","message":"联网已关闭"}}');
  await assert.rejects(f.search.search({ query: 'manual' }), /web_disabled/);
  await assert.rejects(f.fetchPage.fetch({ url: 'https://example.test/manual' }), /web_disabled/);
});

test('batched queries share a bounded transport and cancelled queued work never dispatches', async t => {
  const f = fixture(t);
  let active = 0, peak = 0, count = 0;
  const release = [];
  globalThis.fetch = async () => {
    active++; count++; peak = Math.max(peak, active);
    await new Promise(resolve => release.push(resolve));
    active--;
    return new Response('{"registered_urls":[]}');
  };
  const running = Array.from({ length: 4 }, () => f.search.search({ query: 'manual' }));
  await new Promise(resolve => setImmediate(resolve));
  const controller = new AbortController();
  const cancelled = f.search.search({ query: 'cancelled' }, controller.signal);
  controller.abort();
  await assert.rejects(cancelled);
  const following = f.search.search({ query: 'following' });
  release.splice(0).forEach(resolve => resolve());
  await Promise.all(running);
  await new Promise(resolve => setImmediate(resolve));
  release.splice(0).forEach(resolve => resolve());
  await following;
  assert.equal(peak, 4);
  assert.equal(count, 5);
});
