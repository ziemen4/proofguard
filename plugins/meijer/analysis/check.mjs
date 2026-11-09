/** Bounded, policy-neutral abstract evaluation of the foundation IO interface.
 * Values carry input-position dependencies, never owner labels or permissions.
 * Unknown data branches over every constructor; unsupported computations reject.
 */
import { pathToFileURL } from 'node:url';

class AnalysisError extends Error {}

const MAX_STEPS = 12000;
const MAX_DEPTH = 512;
const MAX_BRANCHES = 128;
const RESERVED = 'pg_program_summary';
// Unfolded instead of summarized: effects must reach the graph and inputs stay positional.
const OPAQUE_TYPES = new Set(['PGIO', 'PGInputs']);
const union = (...sets) => new Set(sets.flatMap(set => [...set]));
const data = (tag, fields = [], control = new Set()) => ({ kind: 'data', tag, fields, control });
const unknown = (dependencies, excluded = []) => ({ kind: 'unknown', dependencies, excluded });

/** Keep dependencies in closures and alternatives, including secret-selected constants. */
function dependencies(value, seen = new Set()) {
  if (seen.has(value)) return new Set();
  seen.add(value);
  switch (value.kind) {
    case 'unknown': return value.dependencies;
    case 'data': return union(value.control, ...value.fields.map(item => dependencies(item, seen)));
    case 'choice': return union(value.control, dependencies(value.yes, seen), dependencies(value.no, seen));
    case 'closure': return union(...value.environment.map(item => dependencies(item, seen)));
    case 'function': return union(...value.arguments.map(item => dependencies(item, seen)));
    default: throw new AnalysisError('Unsupported abstract value');
  }
}

function uncertain(value) {
  return value.kind === 'unknown' || value.kind === 'choice'
    || value.kind === 'data' && value.fields.some(uncertain);
}

function inputShape(value) {
  return value.kind === 'data' && (value.tag === 'PGInput' || value.tag === 'PGNoInputs' || value.fields.some(inputShape));
}

function influenced(value, control) {
  if (value.kind === 'unknown') return unknown(union(value.dependencies, control), value.excluded);
  if (value.kind === 'data' || value.kind === 'choice') return { ...value, control: union(value.control, control) };
  throw new AnalysisError('Input did not resolve to data');
}

function strip(term) {
  while (term.$ === 'Ann') term = term.x;
  return term;
}

class Analyzer {
  constructor(B, book, inputCount) {
    this.B = B;
    this.book = book;
    this.inputCount = inputCount;
    this.steps = 0;
    this.branches = 0;
    this.reads = new Set();
    this.readCache = new Map();
    this.plainCache = new Map();
  }

  tick(depth) {
    if (++this.steps > MAX_STEPS || depth > MAX_DEPTH) {
      throw new AnalysisError('Source analysis limit exceeded');
    }
  }

  choice(control, yes, no) {
    if (++this.branches > MAX_BRANCHES) throw new AnalysisError('Source branch limit exceeded');
    return { kind: 'choice', control, yes, no };
  }

  /** Pure summaries are safe only when no transitive input lookup is hidden. */
  readsInputs(name, visiting = new Set()) {
    if (name === RESERVED) throw new AnalysisError('Program cannot inspect its own analysis binding');
    if (name === 'pg_input') return true;
    if (this.readCache.has(name)) return this.readCache.get(name);
    if (visiting.has(name)) return false;
    const definition = this.book.tlds[name];
    if (definition?.$ !== 'Def' || definition.v === null) return false;
    const next = new Set([...visiting, name]);
    const seen = new Set();
    const walk = term => {
      if (term === null || typeof term !== 'object') return false;
      if (seen.has(term)) return false;
      seen.add(term);
      if (term.$ === 'Ref' && this.readsInputs(term.k, next)) return true;
      if (term.$ === 'Var') return false;
      return Object.entries(term).some(([key, value]) => key !== 's'
        && (Array.isArray(value) ? value.some(walk) : walk(value)));
    };
    const result = walk(this.B.term_lower(definition.v));
    // A cycle-local false result is not memoized: another path may reach pg_input.
    if (result) this.readCache.set(name, true);
    return result;
  }

  /** Plain data is first-order: only closed datatypes, with no effects or inputs inside. */
  isPlain(term, visiting = new Set()) {
    const type = strip(term);
    if (!['Ref', 'ADT'].includes(type.$) || type.x?.length || type.r?.length) return false;
    if (visiting.has(type.k)) return true;
    const family = this.book.tlds[type.k];
    if (OPAQUE_TYPES.has(type.k) || family?.$ !== 'ADT' || family.n !== 0) return false;
    const next = new Set([...visiting, type.k]);
    return family.c.every(constructor => {
      let field = strip(this.B.term_lower(constructor.T));
      for (; field.$ === 'All'; field = strip(field.B)) {
        if (!this.isPlain(field.A, next)) return false;
      }
      return true;
    });
  }

  returnsPlain(name, type) {
    if (!this.plainCache.has(name)) this.plainCache.set(name, this.isPlain(type));
    return this.plainCache.get(name);
  }

  function(name) {
    if (name === RESERVED) throw new AnalysisError('Program cannot inspect its own analysis binding');
    const definition = this.book.tlds[name];
    if (definition?.$ !== 'Def' || definition.v === null || definition.u || definition.i) {
      throw new AnalysisError(`Unsupported source definition: ${name}`);
    }
    let type = strip(this.B.term_lower(definition.T));
    let arity = 0;
    while (type.$ === 'All') { arity++; type = strip(type.B); }
    return { kind: 'function', name, term: this.B.term_lower(definition.v),
      arguments: [], arity, plain: this.returnsPlain(name, type) };
  }

  evaluate(term, environment, control, depth) {
    this.tick(depth);
    switch (term.$) {
      case 'Var': {
        if (!Number.isInteger(term.i) || term.i < 0 || term.i >= environment.length) {
          throw new AnalysisError('Unresolved source variable');
        }
        const value = environment[term.i];
        for (const item of dependencies(value)) this.reads.add(item);
        return value;
      }
      case 'Ref': {
        const value = this.function(term.k);
        return value.arity === 0 ? this.evaluate(value.term, [], control, depth + 1) : value;
      }
      case 'Ctr': return data(term.k, term.x.map(value => this.evaluate(value, environment, control, depth + 1)), control);
      case 'Lam':
      case 'Mat': return { kind: 'closure', term, environment };
      case 'App': return this.apply(this.evaluate(term.f, environment, control, depth + 1),
        this.evaluate(term.x, environment, control, depth + 1), control, depth + 1);
      case 'Let': {
        const values = term.v.map(value => this.evaluate(value, environment, control, depth + 1));
        const extended = environment.slice();
        term.i.forEach((index, i) => { extended[index] = values[i]; });
        return this.evaluate(term.f, extended, control, depth + 1);
      }
      case 'Ann': return this.evaluate(term.x, environment, control, depth + 1);
      case 'Rwt': return this.evaluate(term.f, environment, control, depth + 1);
      case 'Lit': {
        // Text is constant: keep only its length class, without expanding every character.
        if (term.k === 'String') {
          return term.v.length ? data('SCon', [unknown(new Set(control)), unknown(new Set(control))], control)
            : data('SNil', [], control);
        }
        if (typeof term.v === 'number' && term.v > 4096) {
          throw new AnalysisError('Source literal exceeds analysis limit');
        }
        return this.evaluate(this.B.term_lower(this.B.lit_step(term)), environment, control, depth + 1);
      }
      default: throw new AnalysisError(`Unsupported computational term: ${term.$}`);
    }
  }

  apply(fn, argument, control, depth) {
    this.tick(depth);
    if (fn.kind === 'choice') return this.choice(fn.control,
      this.apply(fn.yes, argument, union(control, fn.control), depth + 1),
      this.apply(fn.no, argument, union(control, fn.control), depth + 1));
    if (argument.kind === 'choice') return this.choice(argument.control,
      this.apply(fn, argument.yes, union(control, argument.control), depth + 1),
      this.apply(fn, argument.no, union(control, argument.control), depth + 1));
    if (fn.kind === 'function') {
      const args = [...fn.arguments, argument];
      if (args.length < fn.arity) return { ...fn, arguments: args };
      if (args.length !== fn.arity) throw new AnalysisError('Source application arity mismatch');
      if (fn.name === 'pg_input') {
        const index = this.index(args[0]);
        let cursor = args[1];
        for (let i = 0; i < index; i++) {
          if (cursor.kind !== 'data' || cursor.tag !== 'PGInput') throw new AnalysisError('Input index out of range');
          cursor = cursor.fields[1];
        }
        if (cursor.kind !== 'data' || cursor.tag !== 'PGInput') throw new AnalysisError('Input index out of range');
        // Dependencies use enrolled positions; synthetic input lists add no authority.
        for (const item of dependencies(cursor.fields[0])) this.reads.add(item);
        return influenced(cursor.fields[0], union(control, dependencies(args[0])));
      }
      if (fn.name === 'pg_text' && args[0].kind === 'data') {
        const origin = union(control, dependencies(args[0]));
        return args[0].tag === 'SNil' ? data('PGEndBytes', [], origin)
          : data('PGByte', [unknown(origin), unknown(origin), unknown(origin)], origin);
      }
      // A pure function's result depends only on its arguments, so recursion over
      // unknown data needs no unfolding, and the summary keeps every dependency.
      if (fn.plain && args.some(uncertain) && !args.some(inputShape) && !this.readsInputs(fn.name)) {
        return unknown(union(control, ...args.map(value => dependencies(value))));
      }
      let result = this.evaluate(fn.term, [], control, depth + 1);
      for (const value of args) result = this.apply(result, value, control, depth + 1);
      return result;
    }
    if (fn.kind !== 'closure') throw new AnalysisError('Unknown function application');
    const { term, environment } = fn;
    if (term.$ === 'Lam') {
      const extended = environment.slice();
      extended[term.i] = argument;
      return this.evaluate(term.f, extended, control, depth + 1);
    }
    if (term.$ !== 'Mat') throw new AnalysisError('Unsupported source closure');
    if (argument.kind === 'data') {
      if (argument.tag !== term.k) return this.apply(this.evaluate(term.m, environment, control, depth + 1), argument, control, depth + 1);
      let result = this.evaluate(term.h, environment, union(control, argument.control), depth + 1);
      for (const value of argument.fields) result = this.apply(result, value, union(control, argument.control), depth + 1);
      return result;
    }
    if (argument.kind !== 'unknown') throw new AnalysisError('Unsupported match scrutinee');
    const family = this.B.book_fam(this.book, term.k);
    const constructors = this.book.tlds[family]?.c;
    const constructor = constructors?.find(value => value.k === term.k);
    if (!constructor || argument.excluded.includes(term.k)) throw new AnalysisError('Unsupported source match');
    const influence = union(control, argument.dependencies);
    let yes = this.evaluate(term.h, environment, influence, depth + 1);
    for (let i = 0; i < constructor.n; i++) yes = this.apply(yes, unknown(argument.dependencies), influence, depth + 1);
    const excluded = [...argument.excluded, term.k];
    if (constructors.every(value => excluded.includes(value.k))) return yes;
    const no = this.apply(this.evaluate(term.m, environment, influence, depth + 1),
      unknown(argument.dependencies, excluded), influence, depth + 1);
    return this.choice(influence, yes, no);
  }

  index(value) {
    let result = 0;
    while (value.kind === 'data' && value.tag === 'PGNext' && value.fields.length === 1 && result < 16) {
      result++; value = value.fields[0];
    }
    if (value.kind !== 'data' || value.tag !== 'PGZero' || value.fields.length !== 0 || result >= 16) {
      throw new AnalysisError('Destination or input index is not statically bounded');
    }
    return result;
  }

  shape(value) {
    if (value.kind !== 'data') return 'PGUnknownPayload';
    if (value.tag === 'PGEndBytes') return 'PGEmptyPayload';
    if (value.tag === 'PGByte') return 'PGPresentPayload';
    if (value.tag === 'PGChunks') {
      const shapes = value.fields.map(value => this.shape(value));
      if (shapes.includes('PGPresentPayload')) return 'PGPresentPayload';
      if (shapes.every(value => value === 'PGEmptyPayload')) return 'PGEmptyPayload';
    }
    return 'PGUnknownPayload';
  }

  graph(value, control = new Set(), depth = 0) {
    this.tick(depth);
    if (value.kind === 'choice') return { kind: 'choice',
      yes: this.graph(value.yes, union(control, value.control), depth + 1),
      no: this.graph(value.no, union(control, value.control), depth + 1) };
    if (value.kind !== 'data') throw new AnalysisError('Effect batch is not statically described');
    if (value.tag === 'PGDone' && value.fields.length === 0) return { kind: 'done' };
    // Abort cancels the whole batch, so its conditions decide the visible outcome.
    if (value.tag === 'PGAbort' && value.fields.length === 0) {
      return { kind: 'abort', dependencies: [...union(control, value.control)].sort((a, b) => a - b) };
    }
    if (value.tag !== 'PGWrite' || value.fields.length !== 3) throw new AnalysisError('Unsupported effect constructor');
    const [target, payload, next] = value.fields;
    const deps = union(control, value.control, dependencies(target), dependencies(payload));
    return { kind: 'write', destination: this.index(target), dependencies: [...deps].sort((a, b) => a - b),
      shape: this.shape(payload), next: this.graph(next, control, depth + 1) };
  }

  run(name) {
    const root = this.function(name);
    let type = strip(this.B.term_lower(this.book.tlds[name].T));
    const nominal = (term, name) => ['Ref', 'ADT'].includes(strip(term).$) && strip(term).k === name;
    if (root.arity !== 1 || type.$ !== 'All' || !nominal(type.A, 'PGInputs') || !nominal(type.B, 'PGIO')) {
      throw new AnalysisError('Analyzed implementation must have type PGInputs -> PGIO');
    }
    let inputs = data('PGNoInputs');
    for (let i = this.inputCount - 1; i >= 0; i--) inputs = data('PGInput', [unknown(new Set([i])), inputs]);
    const value = this.apply(root, inputs, new Set(), 0);
    return { effects: this.graph(value), reads: [...this.reads].sort((a, b) => a - b) };
  }
}

/** Verify the source profile and return facts for Meijer's mandatory Bend laws. */
export async function verify({ program, contract, input_count, toolchain }) {
  // Parsing is part of this plugin's trusted analysis, never a foundation AST ABI.
  const B = await import(pathToFileURL(`${toolchain.source}/bend.ts`));
  const book = B.book_nil();
  for (const source of [...contract.sources, ...program]) {
    B.parse_book(book, '', source.content, '', Object.create(null));
  }
  const binding = book.tlds[RESERVED];
  const returnType = binding && B.term_lower(binding.T);
  if (binding?.$ !== 'Def' || binding.n !== 0 || !['Ref', 'ADT'].includes(returnType?.$)
      || returnType.k !== 'PGProgramSummary' || contract.project.implementations.includes(RESERVED)
      || contract.project.proofs.includes(RESERVED)) throw new AnalysisError('Invalid source analysis binding');
  const summary = new Analyzer(B, book, input_count).run('main');
  const ctr = (name, ...fields) => `${name}{${fields.join(', ')}}`;
  const index = number => number === 0 ? ctr('PGZero') : ctr('PGNext', index(number - 1));
  const deps = values => values.reduceRight((rest, value) => ctr('PGDependency', index(value), rest), ctr('PGNoDependencies'));
  const graph = value => value.kind === 'done' ? ctr('PGNoEffects')
    : value.kind === 'abort' ? ctr('PGAbortEffect', deps(value.dependencies))
    : value.kind === 'choice' ? ctr('PGEffectChoice', graph(value.yes), graph(value.no))
    : ctr('PGWriteEffect', index(value.destination), deps(value.dependencies), ctr(value.shape), graph(value.next));
  return [{ name: RESERVED, source: `def ${RESERVED}() -> PGProgramSummary:\n  ${ctr('PGProgramSummary', graph(summary.effects), deps(summary.reads))}\n` }];
}
