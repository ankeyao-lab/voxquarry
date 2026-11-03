// 0.1.5rc1's Python facade exposes create/prompt only. This private runner
// socket calls the official Agent.resume API; the original JSON-RPC server
// continues to deliver native session notifications. No core monkey-patching.
import net from 'node:net';
import { readFileSync, unlinkSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
export const name = 'voxquarry-session-resume';
export const inject = ['agents', 'sessionPersistence'];
export function apply(ctx, config) {
  const request = JSON.parse(readFileSync(config.requestPath, 'utf8'));
  let handle, pending;
  const socket = '/state/voxquarry-resume.sock';
  try { unlinkSync(socket); } catch {}
  const server = net.createServer(connection => {
    let input = '';
    connection.on('data', chunk => {
      input += chunk;
      if (Buffer.byteLength(input) > 4 * 1024 * 1024) return connection.destroy();
      if (!input.endsWith('\n')) return;
      connection.pause();
      Promise.resolve().then(async () => {
        const value = JSON.parse(input);
        const sessionId = request.session_key || request.attempt_id;
        if (!sessionId || value.sessionId !== sessionId) throw new Error('Resume session mismatch');
        if (value.operation === 'exists') return { exists: Boolean(await ctx.sessionPersistence.stat(sessionId)) };
        if (!Array.isArray(value.contentBlocks)) throw new Error('Invalid follow-up content');
        pending ??= ctx.agents.resume({ resumeSessionId: sessionId,
          agentOptions: { provider: 'deepseek-official', model: request.model_id,
            reasoningEffort: request.reasoning_effort, maxTokens: request.max_output_tokens } });
        handle = await pending;
        const message = { id: randomUUID(), role: 'user', source: { kind: 'user' }, content: value.contentBlocks };
        handle.agent.followup(message);
        return { messageId: message.id };
      }).then(value => connection.end(JSON.stringify(value) + '\n'),
        error => connection.end(JSON.stringify({ error: String(error.message) }) + '\n'));
    });
  });
  server.listen(socket);
  ctx.on('dispose', async () => {
    if (pending) await pending.catch(() => {});
    if (handle) await handle.dispose();
    await new Promise(resolve => server.close(resolve));
  });
}
