// Runs ONLY in the networkless analysis container. Reuses official providers;
// no business capability, model key, runner home or host socket is mounted here.
import net from 'node:net';
import { unlinkSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
export const name = 'voxquarry-native-service';
export const inject = ['fs', 'subprocess'];

export function apply(ctx) {
  const processes = new Map();
  const fsMethods = new Set(['resolve', 'stat', 'lstat', 'readText', 'readBytes',
    'readByteRange', 'listDir', 'writeText', 'editText']);
  async function dispatch(request) {
    const { op, method, args = [], id } = request;
    if (op === 'fs' && fsMethods.has(method)) {
      const result = await ctx.fs[method](...args);
      return ['readBytes', 'readByteRange'].includes(method)
        ? { base64: Buffer.from(result).toString('base64') } : result ?? null;
    }
    if (op === 'resolveExecutable') return ctx.subprocess.resolveExecutable(...args);
    if (op === 'spawn') {
      const spec = request.spec;
      // Environment contains no credentials. Do not import DSH runner settings
      // from shell-env into this separate execution world.
      spec.env = Object.fromEntries(Object.entries(spec.env || {}).filter(([k]) =>
        !/^DSH_|KEY|PASSWORD|SECRET|TOKEN/i.test(k)));
      const handle = ctx.subprocess.spawn(spec);
      const key = randomUUID(), record = { handle, result: null, error: null };
      processes.set(key, record);
      handle.done.then(result => { record.result = result; }, error => {
        record.error = { message: String(error.message), code: error.code };
      });
      return { id: key };
    }
    const record = processes.get(id);
    if (!record) throw new Error('Unknown native process');
    if (op === 'poll') return { result: record.result, error: record.error,
      streams: Object.fromEntries(['stdout', 'stderr'].map(stream => [stream, record.handle.collected[stream]?.readFrom(0)])) };
    if (op === 'read') {
      if (!['stdout', 'stderr'].includes(request.stream)) throw new Error('Invalid stream');
      return await record.handle.collected[request.stream].readFrom(request.offset);
    }
    if (op === 'terminate') { record.handle.terminate(); return { terminated: true }; }
    throw new Error('Unsupported native operation');
  }
  const socket = '/tmp/voxquarry-native.sock';
  try { unlinkSync(socket); } catch {}
  const server = net.createServer(connection => {
    let input = '';
    connection.setTimeout(30000, () => connection.destroy());
    connection.on('data', chunk => {
      input += chunk;
      if (Buffer.byteLength(input) > 4 * 1024 * 1024) return connection.destroy();
      if (!input.endsWith('\n')) return;
      connection.pause();
      Promise.resolve().then(() => dispatch(JSON.parse(input))).then(
        result => connection.end(JSON.stringify({ result }) + '\n'),
        error => connection.end(JSON.stringify({ error: { message: String(error.message), code: error.code } }) + '\n'),
      );
    });
  });
  server.listen(socket);
  ctx.on('dispose', () => new Promise(resolve => server.close(resolve)));
}
