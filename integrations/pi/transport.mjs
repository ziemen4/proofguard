import { once } from 'node:events';
import { LIMITS, TIMEOUTS, keys, object, validateContext } from './config.mjs';
import { loadPiAi } from './sdk.mjs';

export async function* readFrames(input) {
  let buffered = Buffer.alloc(0);
  for await (const chunk of input) {
    buffered = Buffer.concat([buffered, Buffer.from(chunk)]);
    let end;
    while ((end = buffered.indexOf(10)) !== -1) {
      if (end + 1 > LIMITS.frame) throw new Error('frame limit');
      const frame = buffered.subarray(0, end);
      buffered = buffered.subarray(end + 1);
      yield JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(frame));
    }
    if (buffered.length >= LIMITS.frame) throw new Error('frame limit');
  }
  if (buffered.length) throw new Error('partial final frame');
}

export class FrameWriter {
  constructor(output) {
    this.output = output;
    this.bytes = 0;
    output.on('error', () => { this.failed = true; });
  }
  write(frame) {
    const line = JSON.stringify(frame) + '\n';
    const size = Buffer.byteLength(line);
    if (this.failed || size > LIMITS.frame || this.bytes + size > LIMITS.stream) throw new Error('output limit');
    this.bytes += size;
    this.output.write(line);
  }
  async flush() {
    if (this.output.writableNeedDrain) await once(this.output, 'drain');
    if (this.failed) throw new Error('output unavailable');
  }
}

export function errorMessage(model, reason = 'owner inference unavailable') {
  return { role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: 'error', errorMessage: reason, timestamp: Date.now() };
}

export class StdioInferenceRelay {
  constructor(input, emit, { timeoutMs = TIMEOUTS.inference } = {}) {
    this.input = input;
    this.emit = emit;
    this.timeoutMs = timeoutMs;
    this.nextId = 1;
    this.listening = this.listen();
  }
  fail() {
    this.failed = true;
    const pending = this.pending;
    if (pending) {
      this.finish();
      pending.stream.push({ type: 'error', reason: 'error', error: errorMessage(pending.model) });
      pending.stream.end();
    }
  }
  finish() {
    const pending = this.pending;
    if (pending) {
      clearTimeout(pending.timer);
      pending.signal?.removeEventListener('abort', pending.onAbort);
      this.pending = undefined;
    }
  }
  async listen() {
    try {
      for await (const frame of readFrames(this.input)) {
        const pending = this.pending;
        if (!pending || frame?.id !== pending.id) throw new Error('unsolicited inference frame');
        if (keys(frame, ['kind', 'id']) && frame.kind === 'inference_error') {
          this.fail();
          return;
        }
        if (!keys(frame, ['kind', 'id', 'event']) || frame.kind !== 'inference_event' || !object(frame.event)) {
          throw new Error('invalid inference frame');
        }
        const event = frame.event;
        const types = ['start', 'text_start', 'text_delta', 'text_end', 'thinking_start', 'thinking_delta',
          'thinking_end', 'toolcall_start', 'toolcall_delta', 'toolcall_end', 'done'];
        if (!types.includes(event.type) || (!pending.started && event.type !== 'start') ||
            pending.started && event.type === 'start') throw new Error('invalid inference lifecycle');
        pending.started = true;
        pending.bytes += Buffer.byteLength(JSON.stringify(event));
        if (pending.bytes > LIMITS.stream) throw new Error('inference output limit');
        if (event.type === 'done') {
          if (!object(event.message) || event.message.role !== 'assistant' || !Array.isArray(event.message.content) ||
              !['stop', 'toolUse', 'length'].includes(event.message.stopReason)) throw new Error('invalid final inference event');
          this.finish();
        }
        // The wire carries deltas; pi-agent-core expects a partial message on each update.
        if (event.type === 'start') pending.partial = event.partial;
        else if (event.type !== 'done' && !event.partial) {
          const index = event.contentIndex;
          const content = pending.partial.content;
          if (event.type.endsWith('_start')) {
            content[index] = event.block;
            delete event.block;
          } else if (event.type === 'text_delta') content[index].text += event.delta;
          else if (event.type === 'thinking_delta') content[index].thinking += event.delta;
          else if (event.type === 'text_end') content[index].text = event.content;
          else if (event.type === 'thinking_end') content[index].thinking = event.content;
          else if (event.type === 'toolcall_end') content[index] = event.toolCall;
          event.partial = pending.partial;
        }
        pending.stream.push(event);
        if (event.type === 'done') pending.stream.end();
      }
      this.fail();
    } catch { this.fail(); }
  }
  async provider() {
    const { createAssistantMessageEventStream } = await loadPiAi();
    return (model, context, options = {}) => {
      const stream = createAssistantMessageEventStream();
      if (this.failed || this.pending || options.signal?.aborted) {
        stream.push({ type: 'error', reason: 'error', error: errorMessage(model) });
        stream.end();
        return stream;
      }
      const id = this.nextId++;
      const onAbort = () => this.fail();
      this.pending = { id, stream, model, signal: options.signal, onAbort, started: false, bytes: 0,
        timer: setTimeout(() => this.fail(), this.timeoutMs) };
      options.signal?.addEventListener('abort', onAbort, { once: true });
      try { this.emit({ kind: 'inference', id, context: validateContext(context) }); }
      catch { this.fail(); }
      return stream;
    };
  }
  close() { this.fail(); this.input.destroy(); }
}
