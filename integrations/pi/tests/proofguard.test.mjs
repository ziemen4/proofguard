/** Offline SDK/transport tests. No provider authentication, inference or fake gate. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, mkdir, writeFile, readFile, readdir, symlink, link, rename, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createOutboxClient, sourceCommitment, validateResult } from '../outbox.mjs';
import { workspaceReadTool, foundationTools, createAuthoringSession, validateTask } from '../proofguard.mjs';
import { createGuestRuntime, runSession } from '../session.mjs';
import { createScenarioProvider, validateScenario } from '../scenario.mjs';
import { StdioInferenceRelay, readFrames, FrameWriter } from '../transport.mjs';
import { serveProviderRelay } from '../provider-relay.mjs';
import { relayEvent, visibleEvent, traceValue } from '../trace.mjs';
import { LIMITS } from '../config.mjs';

const execute = promisify(execFile);
const SOURCE = 'def main(inputs):\n  write_report(inputs)\n';

test('relay omits growing snapshots and preserves final signatures', () => {
  const message = { role: 'assistant', content: [
    { type: 'thinking', thinking: '', thinkingSignature: 'opaque'.repeat(20000) },
    { type: 'toolCall', id: 'call-1', name: 'write_program', arguments: { program: SOURCE } },
  ] };
  const event = { type: 'toolcall_delta', contentIndex: 1, delta: 'abc', partial: message };
  const compact = relayEvent(event);
  assert.equal(compact.delta, 'abc');
  assert.equal(compact.partial, undefined);
  assert.equal(message.content[0].thinkingSignature.length, 120000);
  assert.deepEqual(relayEvent({ type: 'done', reason: 'toolUse', message }),
                   { type: 'done', reason: 'toolUse', message });
  assert.ok(JSON.stringify(compact).length < 1000);
});

async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), 'proofguard-pi-unit-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const task = path.join(root, 'task');
  const outbox = path.join(root, 'outbox');
  const results = path.join(root, 'results');
  await Promise.all([task, outbox, results].map(directory => mkdir(directory)));
  await writeFile(path.join(task, 'brief.txt'), 'ordinary document');
  return { root, task, outbox, results };
}

function task() {
  return { schema: 1, enrollment_id: 'a'.repeat(64), interface: [], entrypoint: 'main',
    instructions: 'Write Bend using the foundation interface.' };
}

test('task accepts the foundation interface and rejects private exports', () => {
  assert.equal(validateTask(task()).schema, 1);
  assert.throws(() => validateTask({ ...task(), contract: { private: 'law' } }));
  assert.throws(() => validateTask({ ...task(), authoring: { gate_managed_laws: ['secret'] } }));
  assert.throws(() => validateTask({ ...task(), schema: 2 }));
  assert.throws(() => validateTask({ ...task(), interface: [{ path: 'effects.bend' }] }));
});

async function publishProtocolFixture(filename, result) {
  const temporary = filename + '.unit-upload';
  await writeFile(temporary, JSON.stringify(result), { flag: 'wx' });
  await rename(temporary, filename);
}

async function sessionFor(t, f, turns, client) {
  const provider = await createScenarioProvider({ schema: 1, turns });
  const { runtime, model } = await createGuestRuntime(provider, { offline: true });
  const session = await createAuthoringSession(runtime, model, client, task(), { workspace: f.task, results: f.results });
  t.after(() => { client.close(); session.dispose(); });
  return session;
}

test('actual pi read tool scopes regular files and rejects final/intermediate symlinks', async t => {
  const f = await fixture(t);
  const outside = path.join(f.root, 'excluded.txt');
  await writeFile(outside, 'synthetic excluded content');
  await symlink(outside, path.join(f.task, 'escape.txt'));
  await symlink(f.task, path.join(f.task, 'alias'));
  const read = await workspaceReadTool(f.task, [f.results]);
  const readAt = filename => read.execute('read', { path: filename });
  assert.equal((await readAt('brief.txt')).content[0].text, 'ordinary document');
  await assert.rejects(readAt(outside));
  await assert.rejects(readAt('escape.txt'));
  await assert.rejects(readAt('alias/brief.txt'));
});

test('actual pi read rejects FIFO without blocking, oversized files and hardlinks', async t => {
  const f = await fixture(t);
  await execute('/usr/bin/mkfifo', [path.join(f.task, 'fifo')]);
  await writeFile(path.join(f.task, 'large'), Buffer.alloc(LIMITS.file + 1));
  await link(path.join(f.task, 'brief.txt'), path.join(f.task, 'hardlink'));
  const read = await workspaceReadTool(f.task, [f.results]);
  let timer;
  try {
    await Promise.race([assert.rejects(read.execute('fifo', { path: 'fifo' }), /file limit/),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('FIFO blocked')), 1000); })]);
  } finally { clearTimeout(timer); }
  await assert.rejects(read.execute('large', { path: 'large' }));
  await assert.rejects(read.execute('hardlink', { path: 'hardlink' }));
});

test('outbox publication is one complete source-only UUID file and missing result fences retries', async t => {
  const f = await fixture(t);
  const events = [];
  const client = createOutboxClient({ ...f, timeoutMs: 100, pollMs: 5, onEvent: event => events.push(event) });
  await assert.rejects(client.submit(SOURCE), /reconciliation/);
  assert.equal(client.terminal, true);
  const names = await readdir(f.outbox);
  assert.equal(names.length, 1);
  assert.match(names[0], /^[a-f0-9]{32}\.bend$/);
  assert.equal(await readFile(path.join(f.outbox, names[0]), 'utf8'), SOURCE);
  assert.equal(events[0].source_id, sourceCommitment(SOURCE));
  await assert.rejects(client.submit(SOURCE));
  assert.deepEqual(await readdir(f.outbox), names);
});

test('outbox cancellation fences all captured tool descriptors with no republish', async t => {
  const f = await fixture(t);
  let published;
  const ready = new Promise(resolve => { published = resolve; });
  const client = createOutboxClient({ ...f, onEvent: published });
  const read = await workspaceReadTool(f.task, [f.results]);
  const [guardedRead, write] = foundationTools(read, client);
  const pending = write.execute('write', { program: SOURCE });
  const rejected = assert.rejects(pending, /reconciliation/);
  await ready;
  client.close();
  await rejected;
  await assert.rejects(guardedRead.execute('queued-read', { path: 'brief.txt' }));
  await assert.rejects(write.execute('queued-write', { program: SOURCE }));
  assert.equal((await readdir(f.outbox)).length, 1);
});

test('result protocol binds source and proposal; this fixture authorizes no effects', () => {
  const proposal = 'b'.repeat(32);
  const source_id = sourceCommitment(SOURCE);
  const result = { schema: 1, proposal, source_id, status: 'rejected', reason: 'unit_protocol_fixture',
    check_id: null, diagnostics: '', archive_durable: null };
  assert.equal(validateResult(result, proposal, source_id), result);
  for (const mutation of [{ ...result, source_id: '0'.repeat(64) }, { ...result, proposal: 'c'.repeat(32) },
    { ...result, status: 'accepted' }, { ...result, status: 'task_consumed' }, { ...result, status: 'unknown' },
    { ...result, authority: '/not-allowed' }]) {
    assert.throws(() => validateResult(mutation, proposal, source_id));
  }
  const unicode = { ...result, diagnostics: '\u{1f642}'.repeat(12000) };
  assert.equal(validateResult(unicode, proposal, source_id), unicode);
  assert.throws(() => validateResult({ ...result, diagnostics: 'a'.repeat(12001) }, proposal, source_id));
  assert.throws(() => validateResult({ ...unicode, diagnostics: unicode.diagnostics + '\u{1f642}' }, proposal, source_id));
});

test('source commitment matches the PGW1 binary fixture', () => {
  assert.equal(sourceCommitment('abc'), '948ce632dd69b04b8ffbdcf6954a5208c5d846d4cd049d0b92116dc4e42204b1');
});

test('immutable result capture tolerates the owner publication hardlink window', async t => {
  const f = await fixture(t);
  let published;
  const ready = new Promise(resolve => { published = resolve; });
  const client = createOutboxClient({ ...f, timeoutMs: 1000, pollMs: 5, onEvent: published });
  const pending = client.submit(SOURCE);
  const { proposal, source_id } = await ready;
  const result = { schema: 1, proposal, source_id, status: 'rejected', reason: 'unit_protocol_fixture',
    check_id: null, diagnostics: '', archive_durable: null };
  const temporary = path.join(f.results, '.publication');
  await writeFile(temporary, JSON.stringify(result));
  await link(temporary, path.join(f.results, `${proposal}.json`));
  assert.equal((await pending).status, 'rejected');
  assert.equal(client.terminal, false);
  client.close();
});

test('completed and rejected results permit fresh explicit submissions and bound result reads', async t => {
  const f = await fixture(t);
  const events = [];
  let notify;
  const client = createOutboxClient({ ...f, timeoutMs: 1000, pollMs: 5, onEvent: event => {
    events.push(event);
    if (event.type === 'proposal') notify(event);
  } });
  t.after(() => client.close());
  let deactivated = 0;
  const tools = foundationTools(await workspaceReadTool(f.task, [f.results]), client, () => { deactivated++; });
  const sources = [SOURCE, SOURCE + '\n', SOURCE];
  const statuses = ['completed', 'rejected', 'completed'];
  const proposals = [];
  for (let index = 0; index < sources.length; index++) {
    const published = new Promise(resolve => { notify = resolve; });
    const pending = tools[1].execute(`explicit-${index}`, { program: sources[index] });
    const { proposal, source_id } = await published;
    proposals.push(proposal);
    assert.equal(source_id, sourceCommitment(sources[index]));
    assert.equal(await readFile(path.join(f.outbox, `${proposal}.bend`), 'utf8'), sources[index]);
    const filename = path.join(f.results, `${proposal}.json`);
    const result = { schema: 1, proposal, source_id, status: statuses[index], reason: 'unit_protocol_fixture',
      check_id: 'd'.repeat(64), diagnostics: null, archive_durable: true };
    // Protocol/lifecycle fixtures do not claim any gate acceptance or effects.
    await publishProtocolFixture(filename, result);
    assert.deepEqual(JSON.parse((await pending).content[0].text), result);
    assert.equal(client.terminal, false);
    assert.equal(client.busy, false);
    assert.equal(deactivated, 0);
    const read = await tools[0].execute(`result-${index}`, { path: filename });
    assert.deepEqual(JSON.parse(read.content[0].text), result);
    assert.equal((await readdir(f.outbox)).length, index + 1);
  }
  assert.equal(new Set(proposals).size, sources.length);
  assert.equal(events.filter(event => event.type === 'proposal_result').length, sources.length);
  client.close();
  await assert.rejects(tools[0].execute('closed-read', { path: 'brief.txt' }));
  await assert.rejects(tools[1].execute('closed-write', { program: SOURCE }));
});

test('uncertain and unknown or obsolete protocol outcomes fence captured tools', async t => {
  for (const status of ['uncertain', 'unknown', 'task_consumed']) {
    const f = await fixture(t);
    let notify;
    const published = new Promise(resolve => { notify = resolve; });
    const client = createOutboxClient({ ...f, timeoutMs: 1000, pollMs: 5, onEvent: notify });
    const read = await workspaceReadTool(f.task, [f.results]);
    let deactivated = 0;
    const tools = foundationTools(read, client, () => { deactivated++; });
    const pending = tools[1].execute('unit-transport', { program: SOURCE });
    const failure = status === 'uncertain' ? undefined : assert.rejects(pending, /reconciliation/);
    const { proposal, source_id } = await published;
    // A transport-state fixture, not evidence that any gate accepted or wrote.
    await publishProtocolFixture(path.join(f.results, `${proposal}.json`), {
      schema: 1, proposal, source_id, status, reason: 'unit_protocol_fixture',
      check_id: 'd'.repeat(64), diagnostics: null, archive_durable: true,
    });
    if (failure) await failure;
    else assert.equal(JSON.parse((await pending).content[0].text).status, status);
    assert.equal(client.terminal, true);
    assert.equal(deactivated, 1);
    await assert.rejects(tools[0].execute('queued-read', { path: 'brief.txt' }));
    await assert.rejects(tools[1].execute('queued-write', { program: SOURCE }));
    assert.equal((await readdir(f.outbox)).length, 1);
  }
});

test('mismatched result source poisons the outbox client without another publication', async t => {
  const f = await fixture(t);
  let notify;
  const published = new Promise(resolve => { notify = resolve; });
  const client = createOutboxClient({ ...f, timeoutMs: 1000, pollMs: 5, onEvent: notify });
  const pending = client.submit(SOURCE);
  const rejection = assert.rejects(pending, /reconciliation/);
  const { proposal } = await published;
  await publishProtocolFixture(path.join(f.results, `${proposal}.json`), {
    schema: 1, proposal, source_id: sourceCommitment(SOURCE + '\n'), status: 'completed',
    reason: null, check_id: null, diagnostics: null, archive_durable: null,
  });
  await rejection;
  await assert.rejects(client.submit(SOURCE));
  assert.equal((await readdir(f.outbox)).length, 1);
});

test('actual SDK exposes exactly read/write_program and emits complete offline message/turn lifecycle', async t => {
  const f = await fixture(t);
  const client = createOutboxClient(f);
  const session = await sessionFor(t, f, [
    { toolCalls: [{ name: 'read', arguments: { path: 'brief.txt' } }] }, { text: 'Offline fixture finished.' },
  ], client);
  assert.deepEqual(session.getActiveToolNames().sort(), ['read', 'write_program']);
  assert.deepEqual(session.agent.state.tools.map(tool => tool.name).sort(), ['read', 'write_program']);
  assert.equal(session.autoRetryEnabled, false);
  const events = [];
  assert.equal(await runSession(session, 'Read the public brief.', { client, onEvent: event => events.push(event) }),
    'Offline fixture finished.');
  const types = events.map(event => event.type);
  for (const type of ['agent_start', 'turn_start', 'message_start', 'message_update', 'message_end',
    'tool_execution_start', 'tool_execution_end', 'turn_end', 'agent_end', 'agent_settled']) assert.ok(types.includes(type), type);
  assert.ok(events.some(event => event.assistantMessageEvent?.type === 'toolcall_delta'));
  assert.ok(events.some(event => event.assistantMessageEvent?.type === 'text_delta'));
  assert.ok(events.filter(event => event.type === 'message_update').every(event => !('partial' in event.assistantMessageEvent)));
  assert.deepEqual(session.getActiveToolNames(), []);
});

test('actual SDK queues two completed requests and result reads before its final answer', async t => {
  const f = await fixture(t);
  const proposals = [];
  const signals = [];
  const published = Array.from({ length: 2 }, () => new Promise(resolve => signals.push(resolve)));
  const events = [];
  const activeAfterResults = [];
  let session;
  const client = createOutboxClient({ ...f, timeoutMs: 2000, pollMs: 5, onEvent: event => {
    events.push(event);
    if (event.type === 'proposal') {
      proposals.push(event);
      signals[proposals.length - 1]?.(event);
    }
    if (event.type === 'proposal_result') activeAfterResults.push(session.getActiveToolNames());
  } });
  const sources = [SOURCE, SOURCE + '\n'];
  const copies = [path.join(f.results, 'first-receipt.json'), path.join(f.results, 'second-receipt.json')];
  session = await sessionFor(t, f, [
    { toolCalls: [
      { name: 'write_program', arguments: { program: sources[0] } },
      { name: 'read', arguments: { path: copies[0] } },
      { name: 'write_program', arguments: { program: sources[1] } },
      { name: 'read', arguments: { path: copies[1] } },
    ] },
    { text: 'Two explicit requests completed; offline conversation finished.' },
  ], client);
  assert.match(session.systemPrompt, /After completed or rejected/);
  assert.doesNotMatch(session.systemPrompt, /task_consumed/);
  const running = runSession(session, 'Exercise repeated explicit requests and read their receipts.', {
    client, onEvent: event => events.push(event),
  });
  const receipts = [];
  for (let index = 0; index < published.length; index++) {
    const { proposal, source_id } = await published[index];
    assert.equal(source_id, sourceCommitment(sources[index]));
    const receipt = { schema: 1, proposal, source_id, status: 'completed', reason: 'unit_protocol_fixture',
      check_id: 'd'.repeat(64), diagnostics: null, archive_durable: true };
    receipts.push(receipt);
    // Read-only copies give the fixed scripted provider known paths. These are
    // transport receipts only, not a mocked checker or evidence of real effects.
    await publishProtocolFixture(copies[index], receipt);
    await publishProtocolFixture(path.join(f.results, `${proposal}.json`), receipt);
  }
  assert.equal(await running, 'Two explicit requests completed; offline conversation finished.');
  assert.equal(proposals.length, 2);
  assert.notEqual(proposals[0].proposal, proposals[1].proposal);
  assert.equal((await readdir(f.outbox)).length, 2);
  const results = session.messages.filter(message => message.role === 'toolResult');
  assert.deepEqual(results.map(message => message.toolName), ['write_program', 'read', 'write_program', 'read']);
  assert.ok(results.every(message => !message.isError));
  for (let index = 0; index < receipts.length; index++) {
    assert.deepEqual(JSON.parse(results[index * 2].content[0].text), receipts[index]);
    assert.deepEqual(JSON.parse(results[index * 2 + 1].content[0].text), receipts[index]);
  }
  assert.ok(activeAfterResults.every(names => [...names].sort().join(',') === 'read,write_program'));
  assert.equal(activeAfterResults.length, 2);
  for (const type of ['agent_start', 'turn_start', 'message_start', 'message_update', 'message_end',
    'tool_execution_start', 'tool_execution_end', 'turn_end', 'agent_end', 'agent_settled']) {
    assert.ok(events.some(event => event.type === type), type);
  }
  assert.equal(events.filter(event => event.type === 'proposal_result').length, 2);
  // Closing after a final answer is distinct from fencing an individual result.
  assert.equal(client.terminal, true);
  assert.deepEqual(session.getActiveToolNames(), []);
});

test('actual SDK queued read/write calls remain fenced after missing-result timeout', async t => {
  const f = await fixture(t);
  const client = createOutboxClient({ ...f, timeoutMs: 50, pollMs: 5 });
  const session = await sessionFor(t, f, [
    { toolCalls: [{ name: 'write_program', arguments: { program: SOURCE } },
      { name: 'read', arguments: { path: 'brief.txt' } },
      { name: 'write_program', arguments: { program: SOURCE } }] },
    { text: 'Outcome unobserved; owner reconciliation is required.' },
  ], client);
  await runSession(session, 'Offline transport cancellation fixture.', { client });
  const results = session.messages.filter(message => message.role === 'toolResult');
  assert.equal(results.length, 3);
  assert.ok(results.every(message => message.isError));
  assert.equal((await readdir(f.outbox)).length, 1);
});

test('actual SDK cancellation closes a waiting outbox tool and leaves terminal tools', async t => {
  const f = await fixture(t);
  let notify;
  const published = new Promise(resolve => { notify = resolve; });
  const client = createOutboxClient({ ...f, onEvent: notify });
  const session = await sessionFor(t, f, [{ toolCalls: [{ name: 'write_program', arguments: { program: SOURCE } }] }], client);
  const cancel = new AbortController();
  const running = runSession(session, 'Cancellation fixture.', { client, signal: cancel.signal });
  const rejected = assert.rejects(running);
  await published;
  cancel.abort();
  await rejected;
  assert.deepEqual(session.getActiveToolNames(), []);
  assert.equal((await readdir(f.outbox)).length, 1);
});

test('stdio relay forwards real SDK fixture events without host tools or model-selected options', async () => {
  const input = new PassThrough();
  const frames = [];
  const relay = new StdioInferenceRelay(input, frame => frames.push(frame));
  const provider = await relay.provider();
  const model = { id: 'unit', api: 'offline', provider: 'offline' };
  const stream = provider(model, { messages: [] });
  assert.deepEqual(frames, [{ kind: 'inference', id: 1, context: { messages: [] } }]);
  const fixtureProvider = await createScenarioProvider({ schema: 1, turns: [{ toolCalls: [{ name: 'write_program', arguments: { program: SOURCE } }] }] });
  for await (const event of fixtureProvider(model, { messages: [] })) {
    input.write(JSON.stringify({ kind: 'inference_event', id: 1, event: relayEvent(event) }) + '\n');
  }
  const events = [];
  for await (const event of stream) events.push(event);
  assert.equal((await stream.result()).content[0].arguments.program, SOURCE);
  assert.equal(events.find(event => event.type === 'toolcall_end').partial.content[0].arguments.program, SOURCE);
  assert.equal(events[0].type, 'start');
  assert.equal(events.at(-1).type, 'done');
  relay.close();
  await relay.listening;
});

test('stdio cross-ID, EOF, error and partial frames poison pending inference without retries', async () => {
  for (const ending of ['wrong-id', 'eof', 'error', 'partial']) {
    const input = new PassThrough();
    const emitted = [];
    const relay = new StdioInferenceRelay(input, frame => emitted.push(frame));
    const provider = await relay.provider();
    const model = { id: 'unit', api: 'offline', provider: 'offline' };
    const pending = provider(model, { messages: [] });
    if (ending === 'wrong-id') input.write('{"kind":"inference_error","id":2}\n');
    if (ending === 'error') input.write('{"kind":"inference_error","id":1}\n');
    if (ending === 'partial') input.end('{"kind":');
    if (ending === 'eof') input.end();
    assert.equal((await pending.result()).stopReason, 'error');
    assert.equal((await provider(model, { messages: [] }).result()).stopReason, 'error');
    assert.equal(emitted.length, 1);
    relay.close();
    await relay.listening;
  }
});

test('owner relay protocol uses a fixed offline provider selection and emits correlated events', async () => {
  const provider = await createScenarioProvider({ schema: 1, turns: [{ text: 'Owner relay fixture.' }] });
  const model = { id: 'owner-fixed', api: 'offline', provider: 'offline', maxTokens: 1024 };
  const input = new PassThrough();
  const output = new PassThrough();
  const frames = [];
  const reading = (async () => { for await (const frame of readFrames(output)) frames.push(frame); })();
  // This runtime is deterministic inference plumbing only, never an authorization gate.
  const runtime = { streamSimple(selected, context, options) {
    assert.equal(selected, model);
    assert.equal(options.maxRetries, 0);
    return provider(selected, context, options);
  } };
  const serving = serveProviderRelay(runtime, model, input, output);
  input.write('{"kind":"inference","id":1,"context":{"messages":[]}}\n');
  while (!frames.some(frame => frame.event?.type === 'done')) await new Promise(resolve => setTimeout(resolve, 5));
  input.end();
  assert.equal(await serving, true);
  output.end();
  await reading;
  assert.ok(frames.every(frame => frame.kind === 'inference_event' && frame.id === 1));
});

test('JSONL framing rejects truncated, invalid UTF-8 and oversized frames', async () => {
  for (const bytes of [Buffer.from('{"kind":'), Buffer.from([0xff, 10]), Buffer.alloc(LIMITS.frame, 32)]) {
    const stream = new PassThrough();
    const consumed = (async () => { for await (const _ of readFrames(stream)) {} })();
    const rejection = assert.rejects(consumed);
    stream.end(bytes);
    await rejection;
  }
  const output = new PassThrough();
  const writer = new FrameWriter(output);
  assert.throws(() => writer.write({ text: 'a'.repeat(LIMITS.frame) }));
  output.destroy();
});

test('scenario schema is explicit offline-only and errors cannot leak credential fields', () => {
  assert.throws(() => validateScenario({ schema: 1, turns: [{ text: 'x', toolCalls: [] }] }));
  assert.throws(() => validateScenario({ schema: 1, turns: [{ toolCalls: [{ name: 'bash', arguments: {} }] }] }));
  const message = { type: 'done', message: { content: [{ type: 'text', text: 'visible' }], stopReason: 'stop' } };
  assert.deepEqual(traceValue(message), message);
  assert.deepEqual(traceValue({ apiKey: 'synthetic', errorMessage: 'synthetic private failure' }),
    { apiKey: '[omitted]', errorMessage: '[omitted]' });
});


test('thousands of streaming deltas use linear wire space on both sides', async () => {
  const input = new PassThrough();
  const relay = new StdioInferenceRelay(input, () => {});
  const provider = await relay.provider();
  const stream = provider({ id: 'unit', api: 'offline', provider: 'offline' }, { messages: [] });
  const message = { role: 'assistant', content: [], stopReason: 'stop' };
  let bytes = 0;
  function send(event) {
    const wire = JSON.stringify({ kind: 'inference_event', id: 1, event: relayEvent(event) }) + '\n';
    bytes += Buffer.byteLength(wire);
    input.write(wire);
  }
  send({ type: 'start', partial: message });
  message.content.push({ type: 'text', text: '' });
  send({ type: 'text_start', contentIndex: 0, partial: message });
  for (let i = 0; i < 8000; i++) {
    message.content[0].text += 'abcd';
    const event = { type: 'text_delta', contentIndex: 0, delta: 'abcd', partial: message };
    send(event);
    const visible = visibleEvent({ type: 'message_update', message, assistantMessageEvent: event });
    assert.equal(visible.message, undefined);
    assert.ok(JSON.stringify(visible).length < 200);
  }
  send({ type: 'text_end', contentIndex: 0, content: message.content[0].text, partial: message });
  send({ type: 'done', reason: 'stop', message });
  let rebuilt;
  for await (const event of stream) if (event.type === 'text_end') rebuilt = event.partial.content[0].text;
  assert.equal(rebuilt, message.content[0].text);
  assert.deepEqual(await stream.result(), message);
  assert.ok(bytes < 1500000, String(bytes));
  relay.close();
  await relay.listening;
});
