const METHODS = { PGGet: 'GET', PGPost: 'POST', PGPut: 'PUT', PGPatch: 'PATCH', PGDelete: 'DELETE' };
const FAILURES = { unreachable: 'PGHttpUnreachable', timeout: 'PGHttpTimeout',
  broken: 'PGHttpBroken', oversized: 'PGHttpOversized' };
const RUN_FAILURES = { unstarted: 'PGRunUnstarted', timeout: 'PGRunTimeout',
  signalled: 'PGRunSignalled', oversized: 'PGRunOversized' };

/** Construct closed typed input data; private bytes are never parsed as code. */
function bytePacker(B) {
  const digits = [...'0123456789ABCDEF'].map(digit => B.Ctr('PGHex' + digit, []));
  const pack = (data, start, end) => {
    if (end - start > 32) {
      const middle = start + Math.floor((end - start) / 2);
      return B.Ctr('PGChunks', [pack(data, start, middle), pack(data, middle, end)]);
    }
    let term = B.Ctr('PGEndBytes', []);
    for (let i = end - 1; i >= start; i--) {
      term = B.Ctr('PGByte', [digits[data[i] >> 4], digits[data[i] & 15], term]);
    }
    return term;
  };
  return encoded => {
    const data = Buffer.from(encoded, 'base64');
    return pack(data, 0, data.length);
  };
}

/** Decode bounded canonical base64 before constructing closed runtime data. */
function checkEncoding(encoded, limit) {
  if (typeof encoded !== 'string' || encoded.length > 4 * Math.ceil(limit / 3)) {
    throw new Error('Invalid private byte encoding');
  }
  const data = Buffer.from(encoded, 'base64');
  if (data.length > limit || data.toString('base64') !== encoded) throw new Error('Noncanonical private bytes');
}

export function runtimeInputTerm(B, inputs) {
  if (!Array.isArray(inputs) || inputs.length > 16) throw new Error('Invalid private inputs');
  const pack = bytePacker(B);
  let result = B.Ctr('PGNoInputs', []);
  for (let i = inputs.length - 1; i >= 0; i--) {
    const encoded = inputs[i];
    checkEncoding(encoded, 65536);
    result = B.Ctr('PGInput', [pack(encoded), result]);
  }
  return result;
}

/** Owner context is a distinct argument, never an extra application input. */
export function runtimeOwnerTerm(B, context) {
  if (context === null || typeof context !== 'object' || Array.isArray(context)
      || Object.keys(context).sort().join(',') !== 'data,schema' || context.schema !== 1) {
    throw new Error('Invalid owner context');
  }
  const pack = bytePacker(B);
  let nodes = 0, size = 0;
  const build = (value, depth) => {
    if (++nodes > 1024 || depth > 128) throw new Error('Private data exceeds shape limit');
    if (value === null) return B.Ctr('PGEmpty', []);
    if (typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid private data');
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === 'bytes_base64') {
      checkEncoding(value.bytes_base64, 4 * 1024 * 1024 + 4096 - size);
      size += Buffer.from(value.bytes_base64, 'base64').length;
      return B.Ctr('PGBlob', [pack(value.bytes_base64)]);
    }
    if (keys.length === 1 && keys[0] === 'pair' && Array.isArray(value.pair) && value.pair.length === 2) {
      return B.Ctr('PGPair', [build(value.pair[0], depth + 1), build(value.pair[1], depth + 1)]);
    }
    throw new Error('Invalid private data');
  };
  return B.Ctr('PGOwnerContext', [build(context.data, 0)]);
}

/** Build the PGResponse a recorded HTTP exchange gives its continuation. */
export function runtimeResponseTerm(B, response) {
  if (response === null || typeof response !== 'object' || Array.isArray(response)) {
    throw new Error('Invalid recorded response');
  }
  const keys = Object.keys(response).sort().join(',');
  if (keys === 'failure') {
    const name = FAILURES[response.failure];
    if (typeof name !== 'string' || !Object.hasOwn(FAILURES, response.failure)) throw new Error('Invalid recorded failure');
    return B.Ctr('PGHttpFailed', [B.Ctr(name, [])]);
  }
  if (keys !== 'body_base64,status' || !Number.isInteger(response.status)
      || response.status < 100 || response.status > 999) throw new Error('Invalid recorded response');
  checkEncoding(response.body_base64, 65536);
  const digits = [...String(response.status)].map(digit => B.Ctr('PGHex' + digit, []));
  return B.Ctr('PGResponse', [B.Ctr('PGStatus', digits), bytePacker(B)(response.body_base64)]);
}

/** Build the PGExit a recorded command gives its continuation. */
export function runtimeExitTerm(B, result) {
  if (result === null || typeof result !== 'object' || Array.isArray(result)) {
    throw new Error('Invalid recorded command result');
  }
  const keys = Object.keys(result).sort().join(',');
  if (keys === 'failure') {
    const name = RUN_FAILURES[result.failure];
    if (typeof name !== 'string' || !Object.hasOwn(RUN_FAILURES, result.failure)) throw new Error('Invalid recorded failure');
    return B.Ctr('PGRunFailed', [B.Ctr(name, [])]);
  }
  if (keys !== 'exit,output_base64' || !Number.isInteger(result.exit) || result.exit < 0 || result.exit > 255) {
    throw new Error('Invalid recorded command result');
  }
  checkEncoding(result.output_base64, 65536);
  const digit = value => B.Ctr('PGHex' + value.toString(16).toUpperCase(), []);
  return B.Ctr('PGExited', [digit(result.exit >> 4), digit(result.exit & 15), bytePacker(B)(result.output_base64)]);
}

/** Leave closed terms as they are; the evaluator supplies Bend's own reduction. */
const CLOSED = { head: term => term, full: term => term };

/** Encode the fixed primitive IO ABI one effect step at a time.
 * Writes before a PGHttp or PGRun form one step. The host dispatches a step
 * before it asks for the next, so only the current step can still be cancelled.
 * Recorded results resume the program past calls and commands it already made:
 * every run restarts from main and re-derives them, which the host compares and
 * never performs again. Continuations are entered only after their result exists.
 */
export function encodePrimitiveIO(value, reduce = CLOSED, responses = []) {
  const ctr = term => {
    if (term?.$ !== 'Ctr' || typeof term.k !== 'string' || !Array.isArray(term.x)) {
      throw new Error('Expected a concrete primitive IO constructor');
    }
    return [term.k, term.x];
  };
  const index = term => {
    for (let i = 0; i < 16; i++) {
      const [name, args] = ctr(term);
      if (name === 'PGZero' && args.length === 0) return i;
      if (name !== 'PGNext' || args.length !== 1) break;
      term = args[0];
    }
    throw new Error('Primitive destination index out of range');
  };
  const digit = term => {
    const [name, args] = ctr(term);
    if (!/^PGHex[0-9A-F]$/.test(name) || args.length !== 0) throw new Error('Invalid byte digit');
    return parseInt(name.slice(5), 16);
  };
  const bytes = term => {
    const data = [];
    const pending = [term];
    let nodes = 0;
    while (pending.length) {
      if (++nodes > 131072) throw new Error('Primitive byte tree exceeds node limit');
      const [name, args] = ctr(pending.pop());
      if (name === 'PGEndBytes' && args.length === 0) continue;
      if (name === 'PGChunks' && args.length === 2) {
        pending.push(args[1], args[0]);
        continue;
      }
      if (name !== 'PGByte' || args.length !== 3 || data.length === 65536) {
        throw new Error('Invalid or oversized primitive bytes');
      }
      data.push(digit(args[0]) * 16 + digit(args[1]));
      pending.push(args[2]);
    }
    return Buffer.from(data).toString('base64');
  };
  const headers = term => {
    const result = [];
    while (true) {
      const [name, args] = ctr(term);
      if (name === 'PGNoHeaders' && args.length === 0) return result;
      if (name !== 'PGHeader' || args.length !== 3 || result.length === 16) throw new Error('Invalid or oversized headers');
      result.push({ name_base64: bytes(args[0]), value_base64: bytes(args[1]) });
      term = args[2];
    }
  };
  const request = term => {
    const [name, args] = ctr(term);
    if (name !== 'PGRequest' || args.length !== 5) throw new Error('Invalid primitive request');
    const [method, unit] = ctr(args[1]);
    if (!Object.hasOwn(METHODS, method) || unit.length !== 0) throw new Error('Invalid request method');
    return { endpoint: index(args[0]), method: METHODS[method], url_base64: bytes(args[2]),
      headers: headers(args[3]), body_base64: bytes(args[4]) };
  };
  const command = args => {
    const values = [];
    let rest = reduce.full(args[1]);
    while (true) {
      const [kind, fields] = ctr(rest);
      if (kind === 'PGNoArgs' && fields.length === 0) break;
      if (kind !== 'PGArg' || fields.length !== 2 || values.length === 16) throw new Error('Invalid or oversized arguments');
      values.push(bytes(fields[0]));
      rest = fields[1];
    }
    return { command: index(reduce.full(args[0])), args_base64: values };
  };
  const completed = [];
  let writes = [];
  const frame = fields => ({ schema: 1, ...(completed.length ? { completed } : {}), ...fields });
  while (true) {
    const [name, args] = ctr(reduce.head(value));
    // The host has not dispatched this step; abort discards all of its writes.
    if (name === 'PGAbort' && args.length === 0) return frame({ abort: true });
    if (name === 'PGDone' && args.length === 0) return frame({ writes });
    if (name === 'PGHttp' && args.length === 2) {
      const http = request(reduce.full(args[0]));
      if (completed.length === responses.length) return frame({ writes, http });
      completed.push({ schema: 1, writes, http });
      value = reduce.resume(args[1], responses[completed.length - 1], 'http');
      writes = [];
      continue;
    }
    if (name === 'PGRun' && args.length === 3) {
      const run = command(args);
      if (completed.length === responses.length) return frame({ writes, run });
      completed.push({ schema: 1, writes, run });
      value = reduce.resume(args[2], responses[completed.length - 1], 'run');
      writes = [];
      continue;
    }
    if (name !== 'PGWrite' || args.length !== 3 || writes.length === 16) {
      throw new Error('Unsupported or oversized primitive IO');
    }
    writes.push({ destination: index(reduce.full(args[0])), bytes_base64: bytes(reduce.full(args[1])) });
    value = args[2];
  }
}
