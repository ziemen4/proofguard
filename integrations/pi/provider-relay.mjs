/** Owner-only authenticated relay. Importing this module never reads credentials. */
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { LIMITS, TIMEOUTS, keys, validateContext } from './config.mjs';
import { FrameWriter, readFrames } from './transport.mjs';
import { loadSdk } from './sdk.mjs';
import { relayEvent } from './trace.mjs';

export async function serveProviderRelay(runtime, model, input, output, reasoning) {
  const writer = new FrameWriter(output);
  let nextId = 1;
  const cancelled = new AbortController();
  const onEnd = () => cancelled.abort();
  input.once('end', onEnd);
  input.once('close', onEnd);
  try {
    for await (const frame of readFrames(input)) {
      if (!keys(frame, ['kind', 'id', 'context']) || frame.kind !== 'inference' || frame.id !== nextId ||
          !Number.isSafeInteger(frame.id) || nextId > LIMITS.turns) throw new Error('invalid relay request');
      const id = nextId++;
      const context = validateContext(frame.context);
      try {
        const signal = AbortSignal.any([cancelled.signal, AbortSignal.timeout(TIMEOUTS.inference)]);
        const stream = runtime.streamSimple(model, context, {
          signal, maxTokens: Math.min(model.maxTokens, 32000), maxRetries: 0,
          ...(reasoning ? { reasoning } : {}),
          timeoutMs: TIMEOUTS.inference, transport: 'sse',
        });
        let done = false;
        for await (const event of stream) {
          if (event.type === 'error') throw new Error('provider unavailable');
          writer.write({ kind: 'inference_event', id, event: relayEvent(event) });
          await writer.flush();
          done = event.type === 'done';
        }
        if (!done) throw new Error('incomplete provider stream');
      } catch {
        writer.write({ kind: 'inference_error', id });
        await writer.flush();
        return false;
      }
    }
    return true;
  } finally {
    cancelled.abort();
    input.off('end', onEnd);
    input.off('close', onEnd);
  }
}

async function main(argv) {
  if (argv.length < 2 || argv.length > 5 || !path.isAbsolute(argv[0]) || !argv[1]) throw new Error('relay arguments');
  const [authDir, modelId, provider = 'openai-codex', reasoning = '', template = ''] = argv;
  const { ModelRuntime } = await loadSdk();
  const runtime = await ModelRuntime.create({ authPath: path.join(authDir, 'auth.json'),
    modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  // The pinned SDK catalog can lag the provider; a template supplies its transport settings.
  const known = runtime.getModel(provider, modelId);
  const base = known ?? (template ? runtime.getModel(provider, template) : undefined);
  if (!base) throw new Error('owner-selected model unavailable');
  const model = known ?? { ...base, id: modelId, name: modelId };
  return await serveProviderRelay(runtime, model, process.stdin, process.stdout, reasoning) ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then(code => { process.exitCode = code; })
    .catch(() => { process.stderr.write('ProofGuard owner relay unavailable.\n'); process.exitCode = 1; });
}
