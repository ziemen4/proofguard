// Only parse source data. This adapter never uses Bend's module loader or runs
// a candidate main. The Python supervisor owns time limits and the kernel call.
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
import { fillObligations } from './obligations.mjs';
import { deferOwnerEntrypoint } from './program.mjs';

const directory = process.argv[2];
const output = process.argv[3];
const B = await import(pathToFileURL(`${directory}/bend.ts`));
const Safe = await import(pathToFileURL(`${directory}/safe.ts`));
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const book = B.book_nil();
let phase = 'parse_contract';

// Ignore parser locations when comparing declarations; all semantic fields,
// including binder quantities and constructor types, remain in the comparison.
function clean(value) {
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(clean);
  return Object.fromEntries(Object.entries(value)
    .filter(([key]) => key !== 's')
    .map(([key, item]) => [key, clean(item)]));
}

function term(value) {
  return clean(B.term_lower(value));
}

/** Describe protected declarations, excluding only replaceable slot bodies. */
function declaration(value, slot = false) {
  if (value.$ === 'ADT') {
    return JSON.stringify({kind: value.$, n: value.n, g: value.g,
      type: term(value.T), constructors: value.c.map(c => ({name: c.k, n: c.n, type: term(c.T)}))});
  }
  return JSON.stringify({kind: value.$, type: term(value.T), templates: value.x,
    body: slot ? null : value.v === null ? null : term(value.v)});
}

/** Let the plugin derive its own representation from captured source text. */
async function verifyPlugin(inputs, ownerDefinitions) {
  if (!Object.hasOwn(inputs, 'plugin_check')) return [];
  const { module, input_count } = inputs.plugin_check;
  if (typeof module !== 'string' || !module.length || !Number.isInteger(input_count)
      || input_count < 0 || input_count > 16) throw new Error('Invalid plugin verification');
  const checker = await import(`data:text/javascript;base64,${Buffer.from(module).toString('base64')}`);
  if (typeof checker.verify !== 'function') throw new Error('Plugin checker must export verify');
  const result = await checker.verify(structuredClone({program: inputs.candidate,
    contract: inputs.contract, input_count, toolchain: {source: directory}}));
  if (!Array.isArray(result)) throw new Error('Plugin verifier must return binding results');
  const bound = new Set();
  for (const binding of result) {
    if (!binding || Object.keys(binding).sort().join(',') !== 'name,source'
        || !ownerDefinitions.has(binding.name) || bound.has(binding.name)
        || book.tlds[binding.name]?.$ !== 'Def' || book.tlds[binding.name].v === null
        || typeof binding.source !== 'string' || !binding.source.length
        || Buffer.byteLength(binding.source) > 65536) {
      throw new Error('Plugin binding must identify a distinct private owner definition');
    }
    // A plugin returns Bend source rather than compiler-internal AST objects.
    // Parse it under the same pinned syntax and retain all declaration guards.
    const start = book.order.length;
    delete book.tlds[binding.name];
    B.parse_book(book, '', binding.source, '', Object.create(null));
    const added = book.order.splice(start);
    if (added.length !== 1 || added[0] !== binding.name) {
      throw new Error('Plugin binding must define exactly its selected owner fact');
    }
    bound.add(binding.name);
  }
  return [...bound];
}

/** Keep verifier-only values out of the admitted application's dependency closure. */
function checkExecutionReferences(forbidden) {
  if (input.execution_entrypoint === undefined) return;
  const definitions = new Set(), constructors = new Set();
  function walk(value) {
    if (value === null || typeof value !== 'object') return;
    if (value.$ === 'Ref' || value.$ === 'ADT') visit(value.k);
    if ((value.$ === 'Ctr' || value.$ === 'Mat') && !constructors.has(value.k)) {
      constructors.add(value.k);
      const constructor = book.ctrs[value.k];
      if (constructor) walk(term(constructor.T));
    }
    if (value.$ === 'Lit') walk(term(B.lit_step(value)));
    for (const [key, child] of Object.entries(value)) {
      if (key !== 's') {
        if (Array.isArray(child)) child.forEach(walk);
        else walk(child);
      }
    }
  }
  function visit(name) {
    if (forbidden.has(name)) reject(`Execution depends on verification-only definition: ${name}`);
    if (definitions.has(name)) return;
    definitions.add(name);
    const declaration = book.tlds[name];
    if (!declaration) return;
    walk(term(declaration.T));
    if (declaration.$ === 'Def' && declaration.v !== null) walk(term(declaration.v));
    if (declaration.$ === 'ADT') declaration.c.forEach(ctr => walk(term(ctr.T)));
  }
  visit(input.execution_entrypoint);
}

function reject(message) {
  throw new Error(message);
}

/** Reject proof holes throughout a term, including erased positions. */
function inspectTerm(value) {
  if (value === null || typeof value !== 'object') return;
  if (value.$ === 'Hol') reject('Proof holes are forbidden, including erased positions');
  for (const child of Object.values(value)) inspectTerm(child);
}

/** Require contract dependencies to exist before candidate declarations load. */
function inspectProtectedTerm(value) {
  if (value === null || typeof value !== 'object') return;
  // Checking after candidate parsing would let an agent supply the meaning of
  // an undeclared dependency in an otherwise byte-identical protected law.
  if (value.$ === 'Lit') {
    // Literals hide constructor dependencies (e.g. Nat uses Zero/Succ and
    // String uses SNil/SCon/Chr). Inspect their pinned desugaring as well.
    if (value.k === 'Nat' && value.v > 4096) {
      // safe.ts additionally factors large naturals through these helpers.
      for (const name of ['Nat.mul', 'Nat.add']) {
        if (!Object.hasOwn(book.tlds, name)) reject(`Undeclared protected dependency: ${name}`);
      }
    }
    inspectProtectedTerm(term(B.lit_step(value)));
    return;
  }
  if (['Ref', 'ADT'].includes(value.$) && !Object.hasOwn(book.tlds, value.k)) {
    reject(`Undeclared protected dependency: ${value.k}`);
  }
  if (['Ctr', 'Mat'].includes(value.$) && !Object.hasOwn(book.ctrs, value.k)) {
    reject(`Undeclared protected constructor: ${value.k}`);
  }
  if (value.$ === 'ADT') {
    for (const name of value.r) {
      if (!Object.hasOwn(book.ctrs, name)) reject(`Undeclared protected constructor: ${name}`);
    }
  }
  for (const child of Object.values(value)) inspectProtectedTerm(child);
}

try {
  for (const source of input.contract.sources) {
    B.parse_book(book, '', source.content, '', Object.create(null));
  }
  const finishOwner = deferOwnerEntrypoint(book, input);
  const slots = new Set([...input.contract.project.implementations, ...input.contract.project.proofs]);
  const open = Object.entries(book.tlds).filter(([, t]) => t.$ === 'Def' && t.v === null).map(([k]) => k);
  if (open.length !== slots.size || open.some(k => !slots.has(k))) {
    reject('Declared implementation/proof slots must equal all open contract definitions');
  }
  for (const value of Object.values(book.tlds)) {
    inspectProtectedTerm(term(value.T));
    if (value.$ === 'Def' && value.v !== null) inspectProtectedTerm(term(value.v));
    if (value.$ === 'ADT') value.c.forEach(c => inspectProtectedTerm(term(c.T)));
  }
  const ownerDefinitions = new Set(Object.entries(book.tlds)
    .filter(([name, value]) => value.$ === 'Def' && value.v !== null && !slots.has(name))
    .map(([name]) => name));
  const protectedDeclarations = new Map(Object.entries(book.tlds)
    .map(([name, value]) => [name, declaration(value, slots.has(name))]));

  phase = 'parse_candidate';
  const candidateStart = book.order.length;
  for (const source of input.candidate) {
    B.parse_book(book, '', source.content, '', Object.create(null));
  }
  finishOwner();
  for (const [name, before] of protectedDeclarations) {
    if (declaration(book.tlds[name], slots.has(name)) !== before) {
      reject(`Protected declaration changed: ${name}`);
    }
  }
  phase = 'plugin_verification';
  const beforePlugin = new Map(Object.entries(book.tlds)
    .map(([name, value]) => [name, { full: declaration(value), type: declaration(value, true) }]));
  const orderBeforePlugin = JSON.stringify(book.order);
  const bindings = await verifyPlugin(input, ownerDefinitions);
  const bound = new Set(bindings);
  if (Object.keys(book.tlds).length !== beforePlugin.size || JSON.stringify(book.order) !== orderBeforePlugin) {
    reject('Plugin verifier changed program declarations or order');
  }
  for (const [name, before] of beforePlugin) {
    if (!book.tlds[name] || declaration(book.tlds[name], bound.has(name)) !== (bound.has(name) ? before.type : before.full)) {
      reject(`Plugin verifier changed protected program: ${name}`);
    }
  }
  phase = 'execution_dependencies';
  checkExecutionReferences(new Set([...bound, ...input.contract.project.proofs]));
  phase = 'parse_candidate';
  fillObligations(B, book, input, candidateStart);
  for (const [name, value] of Object.entries(book.tlds)) {
    if (value.u || value.i) reject(`Unsafe or foreign definition: ${name}`);
    if (value.$ === 'Def' && value.v === null) reject(`Unfilled definition: ${name}`);
    inspectTerm(term(value.T));
    if (value.$ === 'Def' && value.v !== null) inspectTerm(term(value.v));
    if (value.$ === 'ADT') value.c.forEach(c => inspectTerm(term(c.T)));
  }
  phase = 'check_definitions';
  B.book_valid(book);
  if (book.hols !== 0) reject('Incomplete proof obligations');
  phase = 'kernel_translation';
  const unsupported = Safe.safe_emit(book, output);
  if (unsupported.length) reject(`Kernel translation unsupported: ${unsupported.join('')}`);
  process.stdout.write(JSON.stringify({schema: 1, status: 'source_checked',
    implementations: input.contract.project.implementations, proofs: input.contract.project.proofs, bindings}) + '\n');
} catch (error) {
  // Diagnostics are feedback, not executable instructions or authorization.
  const bendError = error?.$ === 'Err';
  const message = bendError ? B.err_show(error) : String(error);
  process.stderr.write(JSON.stringify({schema: 1, status: 'rejected',
    phase, definition: bendError && typeof error.def === 'string' ? error.def : null,
    error_kind: bendError ? 'bend_error' : 'adapter_error', message: message.slice(0, 12000)}) + '\n');
  process.exitCode = 1;
}
