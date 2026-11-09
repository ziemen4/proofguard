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

/** Encode the fixed primitive IO ABI after Bend has computed every value. */
export function encodePrimitiveIO(value) {
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
  const writes = [];
  while (true) {
    const [name, args] = ctr(value);
    // The host has not dispatched any prefix; abort discards the whole batch.
    if (name === 'PGAbort' && args.length === 0) return { schema: 1, abort: true };
    if (name === 'PGDone' && args.length === 0) return { schema: 1, writes };
    if (name !== 'PGWrite' || args.length !== 3 || writes.length === 16) {
      throw new Error('Unsupported or oversized primitive IO');
    }
    writes.push({ destination: index(args[0]), bytes_base64: bytes(args[1]) });
    value = args[2];
  }
}
