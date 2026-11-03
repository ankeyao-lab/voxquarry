// Trusted, read-only Harness plugin. Every model-facing operation leaves this
// process through a task-scoped broker; there is no local filesystem/shell tool.
import { readFileSync } from 'node:fs';

export const name = 'voxquarry-research-tools';
export const inject = ['tools'];

export function apply(ctx, config) {
  const tools = JSON.parse(readFileSync(config.catalogPath, 'utf8'));
  const token = readFileSync(config.tokenPath, 'utf8').trim();
  const base = new URL(config.brokerUrl);
  const request = config.requestPath ? JSON.parse(readFileSync(config.requestPath, 'utf8')) : {};
  const timeoutMs = Math.max(345000, ((request.model_timeout_seconds || 0) + 45) * 1000);
  if (base.protocol !== 'http:' || !['relay', '127.0.0.1'].includes(base.hostname)) {
    throw new Error('VoxQuarry broker must be the fixed local relay');
  }
  for (const tool of tools) {
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(tool.name)) throw new Error('Invalid tool name');
    ctx.tools.register({
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      isConcurrencySafe() { return tool.concurrency_safe === true; },
      output: {
        schema: { type: 'string' },
        render(_args, value) { return [{ type: 'text', text: value }]; },
      },
      async execute(args, exec) {
        const controller = new AbortController();
        // Composite tools can include a full model review. The same trusted
        // runtime configuration sets this wait and the relay's outer deadline.
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        const signal = exec.signal ? AbortSignal.any([exec.signal, controller.signal]) : controller.signal;
        try {
          const response = await fetch(new URL('tools/' + tool.name, base), {
            method: 'POST', redirect: 'error', signal,
            headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
            body: JSON.stringify({ arguments: args, tool_call_id: String(exec.id || exec.callId || '') }),
          });
          const reader = response.body.getReader();
          const chunks = [];
          let count = 0;
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            count += value.byteLength;
            if (count > 1048576) { await reader.cancel(); throw new Error('Tool result exceeds limit'); }
            chunks.push(value);
          }
          const body = Buffer.concat(chunks).toString('utf8');
          if (!response.ok) throw new Error('Tool rejected: ' + body.slice(0, 1500));
          const result = JSON.parse(body);
          // HTTP success only confirms transport. Let the official harness
          // mark a rejected operation as isError, with its repair contract.
          if (result?.error || result?.operation_success === false) {
            throw new Error(JSON.stringify({...result, operation_success: false, error: result.error || {
              code: 'tool_failed', message: 'Tool operation failed', retryable: false,
            }}));
          }
          // The official ToolRunContext marks this successful tool execution
          // as concluding the turn. A JSON field alone is only tool content.
          // Rejected drafts must remain in the loop so the model can repair them.
          if (tool.name === 'submit_answer' && result?.accepted === true && !result.error) {
            exec.concludeTurn();
          }
          return body;
        } finally { clearTimeout(timer); }
      },
    });
  }
}
