export const TOOL_NAMES = Object.freeze(['read', 'write_program']);
export const LIMITS = Object.freeze({ program: 16384, file: 65536, result: 131072,
  task: 2097152, scenario: 262144, brief: 32768, answer: 65536,
  context: 4194304, frame: 8388608, stream: 134217728, response: 4194304,
  turns: 12, proposals: 24 });
export const TIMEOUTS = Object.freeze({ inference: 280000, result: 180000,
  poll: 25, session: 590000 });

export function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function keys(value, expected) {
  return object(value) && Object.keys(value).sort().join(',') === [...expected].sort().join(',');
}

export function boundedText(value, limit) {
  return typeof value === 'string' && value.length > 0 && !value.includes('\0') &&
    Buffer.byteLength(value) <= limit && Buffer.from(value).toString('utf8') === value;
}

export function validateContext(context) {
  if (!object(context) || Object.keys(context).some(key => !['systemPrompt', 'messages', 'tools'].includes(key)) ||
      !Array.isArray(context.messages) || context.messages.length > 256 ||
      (context.systemPrompt !== undefined && typeof context.systemPrompt !== 'string') ||
      (context.tools !== undefined && (!Array.isArray(context.tools) || context.tools.length > 2 ||
        new Set(context.tools.map(tool => tool?.name)).size !== context.tools.length ||
        context.tools.some(tool => !object(tool) || !TOOL_NAMES.includes(tool.name) || !object(tool.parameters)))) ||
      Buffer.byteLength(JSON.stringify(context)) > LIMITS.context) throw new Error('invalid inference context');
  return context;
}

// The launcher must also sanitize Node's startup environment (notably NODE_OPTIONS).
export function fixEnvironment() {
  for (const name of Object.keys(process.env)) delete process.env[name];
  Object.assign(process.env, { PATH: '/usr/bin:/bin', HOME: '/home/agent', TMPDIR: '/tmp',
    PI_CODING_AGENT_DIR: '/nonexistent', PI_OFFLINE: '1', DO_NOT_TRACK: '1',
    PI_TELEMETRY: '0', OTEL_SDK_DISABLED: 'true', NO_COLOR: '1', NODE_ENV: 'production' });
}
