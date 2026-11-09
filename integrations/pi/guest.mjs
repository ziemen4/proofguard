import { pathToFileURL } from 'node:url';
import { LIMITS, TOOL_NAMES, fixEnvironment } from './config.mjs';
import { readRegular, utf8 } from './files.mjs';
import { FrameWriter, StdioInferenceRelay } from './transport.mjs';
import { createScenarioProvider } from './scenario.mjs';
import { createOutboxClient, createAuthoringSession, validateTask } from './proofguard.mjs';
import { createGuestRuntime, runSession } from './session.mjs';
import { traceValue } from './trace.mjs';

async function runGuest(argv, { input = process.stdin, output = process.stdout } = {}) {
  fixEnvironment();
  const writer = new FrameWriter(output);
  const emit = frame => writer.write(frame);
  const onEvent = event => emit({ kind: 'trace', event: traceValue(event) });
  const mode = argv.length === 3 ? 'scenario' : 'relay';
  let session, client, relay;
  let answer = '';
  let conversationCompleted = false;
  const cancel = new AbortController();
  const stop = () => { cancel.abort(); client?.close(); void session?.abort(); };
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  try {
    if (argv.length < 2 || argv.length > 3 || argv[0] !== '/task/task.json' || argv[1] !== '/task/brief.txt' ||
        argv[2] !== undefined && argv[2] !== '/task/scenario.json') throw new Error('invalid guest paths');
    const task = validateTask(JSON.parse(utf8(await readRegular(argv[0], { limit: LIMITS.task }))));
    const brief = utf8(await readRegular(argv[1], { limit: LIMITS.brief }));
    let provider;
    if (mode === 'scenario') {
      provider = await createScenarioProvider(JSON.parse(utf8(await readRegular(argv[2], { limit: LIMITS.scenario }))));
    } else {
      relay = new StdioInferenceRelay(input, emit);
      provider = await relay.provider();
    }
    const { runtime, model } = await createGuestRuntime(provider, { offline: mode === 'scenario' });
    client = createOutboxClient({ onEvent });
    session = await createAuthoringSession(runtime, model, client, task);
    onEvent({ type: 'session_configuration', mode, offlineFixture: mode === 'scenario',
      tools: session.getActiveToolNames(), systemPrompt: session.systemPrompt, task });
    answer = await runSession(session, brief, { client, onEvent, signal: cancel.signal });
    conversationCompleted = true;
  } catch {
    answer = '';
  } finally {
    process.off('SIGTERM', stop);
    process.off('SIGINT', stop);
    client?.close();
    session?.setActiveToolsByName([]);
    relay?.close();
    session?.dispose();
  }
  emit({ kind: 'complete', answer, tools: [...TOOL_NAMES], activeTools: [], completed: conversationCompleted, mode,
    ...(conversationCompleted ? {} : { error: 'guest_failed' }) });
  await writer.flush();
  return conversationCompleted ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runGuest(process.argv.slice(2)).then(code => { process.exitCode = code; })
    .catch(() => { process.stderr.write('ProofGuard guest failed; owner reconciliation may be required.\n'); process.exitCode = 1; });
}
