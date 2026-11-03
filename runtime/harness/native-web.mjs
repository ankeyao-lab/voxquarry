// Official DSH web tools, backed by the existing task-scoped VoxQuarry egress.
// Provider credentials and public URL/DNS checks remain on the Agent server.
import { readFileSync } from 'node:fs';

export const name = 'voxquarry-native-web';
export const inject = ['web'];

export function apply(ctx, config) {
  const token = readFileSync(config.tokenPath, 'utf8').trim();
  const base = new URL(config.brokerUrl);
  if (base.protocol !== 'http:' || !['relay', '127.0.0.1'].includes(base.hostname)) {
    throw new Error('Web provider requires the fixed local relay');
  }
  let active = 0;
  const waiting = [];
  async function acquire(signal) {
    signal?.throwIfAborted();
    if (active < 4) { active++; return; }
    await new Promise((resolve, reject) => {
      const entry = { grant() { signal?.removeEventListener('abort', abort); resolve(); } };
      function abort() {
        const index = waiting.indexOf(entry);
        if (index >= 0) waiting.splice(index, 1);
        reject(signal.reason);
      }
      waiting.push(entry);
      signal?.addEventListener('abort', abort, { once: true });
    });
  }
  async function call(name, args, signal) {
    await acquire(signal);
    try {
      signal?.throwIfAborted();
      const response = await fetch(new URL('tools/' + name, base), {
        method: 'POST', redirect: 'error', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(90000)]) : AbortSignal.timeout(90000),
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ arguments: args }),
      });
      const reader = response.body.getReader();
      const chunks = [];
      let length = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > 1048576) { await reader.cancel(); throw new Error('Web result exceeds limit'); }
        chunks.push(value);
      }
      if (!response.ok) throw new Error('Web broker rejected request (HTTP ' + response.status + ')');
      const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (result.error) throw new Error(result.error.code + ': ' + result.error.message);
      return result;
    } finally {
      const next = waiting.shift();
      if (next) next.grant();
      else active--;
    }
  }
  ctx.web.registerSearchProvider({
    id: 'voxquarry-public', available: () => true,
    async search(request, signal) {
      const result = await call('search_web', { query: request.query }, signal);
      return { answer: JSON.stringify(result),
        sources: (result.registered_urls || []).map(url => ({ url,
          snippet: result.source_id ? '[' + result.source_id + ']' : '' })), truncated: false };
    },
  });
  ctx.web.registerFetchProvider({
    id: 'voxquarry-public', available: () => true,
    async fetch(request, signal) {
      const result = await call('read_web_page', { url: request.url }, signal);
      return { url: result.url || request.url, statusCode: result.approved === false ? 403 : 200,
        body: { kind: 'text', content: JSON.stringify(result) }, truncated: false };
    },
  });
}
