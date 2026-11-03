// Transport adapters only. Model-facing tools, edit guards, Bash jobs, retries
// and compaction remain the pinned official implementations.
import { readFileSync } from 'node:fs';
import { isAbsolute, resolve, relative } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
export const name = 'voxquarry-native-providers';

export function apply(ctx, config) {
  const token = readFileSync(config.tokenPath, 'utf8').trim();
  const endpoint = new URL('http://relay:9000/broker/native');
  async function rpc(value, signal) {
    const response = await fetch(endpoint, { method: 'POST', redirect: 'error',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(45000)]) : AbortSignal.timeout(45000),
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
      body: JSON.stringify(value) });
    const body = await response.json();
    if (!response.ok || body.error) {
      const error = new Error(body.error?.message || body.error?.code || 'Native sandbox unavailable');
      error.code = body.error?.code;
      throw error;
    }
    return body.result;
  }
  const fs = {
    processPath: target => String(target.targetKey),
    processPathFromHostPath: path => isAbsolute(path) ? resolve(path) : undefined,
    fileUrl: target => pathToFileURL(String(target.targetKey)).href,
    contains: (parent, child) => { const path = relative(String(parent.targetKey), String(child.targetKey)); return !path || !path.startsWith('..') && !isAbsolute(path); },
    resolve: (path, opts) => rpc({ op: 'fs', method: 'resolve', args: [path, { cwd: opts?.cwd }] }, opts?.signal),
    stat: async (target, signal) => (await rpc({ op: 'fs', method: 'stat', args: [target] }, signal)) ?? undefined,
    lstat: async (path, opts, signal) => (await rpc({ op: 'fs', method: 'lstat', args: [path, { cwd: opts?.cwd }] }, signal)) ?? undefined,
    readText: (target, signal) => rpc({ op: 'fs', method: 'readText', args: [target] }, signal),
    readBytes: async (target, signal, maxBytes) => Buffer.from((await rpc({ op: 'fs', method: 'readBytes', args: [target, null, maxBytes] }, signal)).base64, 'base64'),
    readByteRange: async (target, range, signal) => Buffer.from((await rpc({ op: 'fs', method: 'readByteRange', args: [target, range] }, signal)).base64, 'base64'),
    listDir: (target, signal) => rpc({ op: 'fs', method: 'listDir', args: [target] }, signal),
    writeText: (target, content, expected, signal) => rpc({ op: 'fs', method: 'writeText', args: [target, content, expected] }, signal),
    editText: (target, edit, expected, signal) => rpc({ op: 'fs', method: 'editText', args: [target, edit, expected] }, signal),
    async streamText(target, signal) {
      return (async function* () {
        const decoder = new TextDecoder();
        for (let offset = 0; ; offset += 65536) {
          const bytes = await fs.readByteRange(target, { offset, length: 65536 }, signal);
          if (!bytes.length) break;
          const text = decoder.decode(bytes, { stream: true });
          if (text.includes('\0')) { const error = new Error('File is not text'); error.code = 'FS_NOT_TEXT'; throw error; }
          yield text;
        }
        const tail = decoder.decode(); if (tail) yield tail;
      })();
    },
  };
  ctx.provide('fs', fs);
  const live = new Set();
  const subprocess = {
    resolveExecutable: (command, env, signal) => rpc({ op: 'resolveExecutable', args: [command, env] }, signal),
    spawn(spec) {
      if (spec.stdio?.stdout === 'pipe' || spec.stdio?.stderr === 'pipe') throw new Error('Raw pipes are not enabled in the research profile');
      const { signal, ...data } = spec;
      const ready = rpc({ op: 'spawn', spec: data });
      let killed = false;
      const streams = { stdout: { text: '', nextOffset: 0, lossy: false }, stderr: { text: '', nextOffset: 0, lossy: false } };
      const call = async (op, rest = {}) => rpc({ op, id: (await ready).id, ...rest });
      const terminate = () => { killed = true; return call('terminate'); };
      const onAbort = () => { terminate().catch(() => {}); };
      signal?.addEventListener('abort', onAbort, { once: true });
      if (signal?.aborted) onAbort();
      const done = (async () => {
        await ready;
        if (killed) await terminate();
        while (true) {
          const state = await call('poll');
          Object.assign(streams, state.streams);
          if (state.error) throw Object.assign(new Error(state.error.message), { code: state.error.code });
          if (state.result) return state.result;
          await sleep(200);
        }
      })().finally(() => { signal?.removeEventListener('abort', onAbort); live.delete(handle); });
      const handle = { done, terminate, waitForExit: () => done.then(() => {}, () => {}),
        collected: Object.fromEntries(['stdout', 'stderr'].map(stream => [stream, {
          readFrom(offset) {
            const snapshot = streams[stream];
            if (offset === 0) return { ...snapshot };
            const bytes = Buffer.from(snapshot.text), start = snapshot.nextOffset - bytes.length;
            return { ...snapshot, text: bytes.subarray(Math.max(0, offset - start)).toString('utf8'),
              lossy: snapshot.lossy && offset < start };
          },
        }])) };
      done.catch(() => {});
      live.add(handle);
      return handle;
    },
  };
  ctx.provide('subprocess', subprocess);
  ctx.on('dispose', () => Promise.allSettled([...live].map(async handle => { await handle.terminate(); await handle.waitForExit(); })));
}
