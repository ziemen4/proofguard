import { LIMITS, TIMEOUTS, TOOL_NAMES, boundedText } from './config.mjs';
import { loadSdk } from './sdk.mjs';
import { visibleEvent } from './trace.mjs';

function emptyResources(runtime, systemPrompt) {
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => systemPrompt,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => { throw new Error('resources disabled'); },
    reload: async () => {},
  };
}

export async function createGuestRuntime(streamSimple, { offline = false } = {}) {
  const sdk = await loadSdk();
  const runtime = await sdk.ModelRuntime.create({
    credentials: { read: async () => undefined, list: async () => [], modify: async () => { throw new Error('credentials disabled'); } },
    modelsPath: null, allowModelNetwork: false, refreshOnCreate: false,
  });
  const provider = offline ? 'proofguard-offline-fixture' : 'proofguard-owner-relay';
  const model = { id: provider, name: provider, api: provider, provider,
    baseUrl: 'http://unused.invalid', reasoning: false, input: ['text'], contextWindow: 131072, maxTokens: 8192,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  runtime.registerProvider(provider, { api: model.api, baseUrl: model.baseUrl,
    apiKey: 'public-local-provider-placeholder', streamSimple, models: [model] });
  return { runtime, model };
}

export async function createConfiguredSession(modelRuntime, model, { tools, systemPrompt, cwd = '/task' }) {
  const sdk = await loadSdk();
  let session;
  const deactivate = () => session?.setActiveToolsByName([]);
  const customTools = tools(deactivate);
  if (customTools.map(tool => tool.name).join(',') !== TOOL_NAMES.join(',')) throw new Error('unexpected tool definitions');
  ({ session } = await sdk.createAgentSession({
    cwd, agentDir: '/nonexistent', modelRuntime, model, thinkingLevel: 'off', scopedModels: [],
    tools: [...TOOL_NAMES], customTools,
    resourceLoader: emptyResources(sdk.createExtensionRuntime(), systemPrompt),
    sessionManager: sdk.SessionManager.inMemory(cwd),
    settingsManager: sdk.SettingsManager.inMemory({ compaction: { enabled: false },
      retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0, timeoutMs: TIMEOUTS.inference } },
      enableAnalytics: false, enableInstallTelemetry: false, enableSkillCommands: false,
      defaultProjectTrust: 'never', images: { blockImages: true }, transport: 'sse',
      packages: [], extensions: [], skills: [], prompts: [], themes: [] }),
  }));
  if (session.getActiveToolNames().sort().join(',') !== [...TOOL_NAMES].sort().join(',') ||
      session.agent.state.tools.map(tool => tool.name).sort().join(',') !== [...TOOL_NAMES].sort().join(',')) {
    session.dispose();
    throw new Error('unexpected SDK tool surface');
  }
  return session;
}

export async function runSession(session, brief, { client, onEvent = () => {}, signal,
  timeoutMs = TIMEOUTS.session } = {}) {
  let turns = 0;
  let bytes = 0;
  let failed = false;
  const abort = () => {
    failed = true;
    client?.close();
    void session.abort();
  };
  const timer = setTimeout(abort, timeoutMs);
  signal?.addEventListener('abort', abort, { once: true });
  const unsubscribe = session.subscribe(event => {
    try {
      if (event.type === 'turn_start' && ++turns > LIMITS.turns) throw new Error('turn limit');
      if (event.type === 'message_update') {
        const delta = event.assistantMessageEvent?.delta;
        if (typeof delta === 'string') bytes += Buffer.byteLength(delta);
        if (bytes > LIMITS.response) throw new Error('response limit');
      }
      if (event.type === 'message_end' && Buffer.byteLength(JSON.stringify(event.message)) > LIMITS.response) {
        throw new Error('message limit');
      }
      onEvent(visibleEvent(event));
    } catch { abort(); }
  });
  try {
    if (signal?.aborted) abort();
    if (failed || !boundedText(brief, LIMITS.brief)) throw new Error('invalid or cancelled brief');
    await session.prompt(brief, { expandPromptTemplates: false });
    const last = session.messages.at(-1);
    if (failed || last?.role !== 'assistant' || last.stopReason !== 'stop' ||
        last.content.some(part => part.type === 'toolCall')) throw new Error('no final answer');
    const answer = last.content.filter(part => part.type === 'text').map(part => part.text).join('\n');
    if (!boundedText(answer, LIMITS.answer)) throw new Error('invalid answer');
    return answer;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    unsubscribe();
    // Final-answer completion closes this conversation even when every owner
    // request completed normally; it does not consume or revoke enrollment.
    client?.close();
    session.setActiveToolsByName([]);
  }
}
