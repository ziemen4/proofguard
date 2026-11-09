# Write a plugin

A plugin lives in its own directory under `plugins/`, with its code, docs and
tests. It may use the foundation's Python API but must not import a harness.
Read [Plugins](../concepts/plugins.md) first for how the pieces fit.

## 1. Manifest and sources

```text
plugins/example/
  plugin.json
  checks.bend
  tests.json
  tests/test_example.py
```

```json
{
  "schema": 1,
  "name": "example",
  "intent": "Only publish input 0 to destination 0",
  "sources": ["checks.bend"],
  "bindings": [],
  "laws": ["example_rule"],
  "entrypoint": "main"
}
```

All manifest fields are described in [Configuration](../reference/configuration.md#plugin-manifest).

## 2. Laws

A law is a named equality in a plugin source. It can refer to the agent's `main`
and to anything the plugin defines. The checker proves a law automatically when
both sides reduce to the same term, so write laws as computations that evaluate to
a fixed value. Meijer's laws all have this shape:

```bend
law scope_safe:
  {mj_both(dependencies_scoped(program_reads()), every_effect(Scopes{}, program_effects())) == Yes{} : Decision}
```

A law the checker cannot prove this way rejects the submission.

The foundation supplies its IO types and the `main` declaration; plugin sources
must not redefine them.

## 3. Optional verifier

Add `"checker": "check.mjs"` when a law needs facts about the program's structure.
The module exports one function:

```js
export async function verify({ program, contract, input_count, toolchain }) {
  const facts = analyze(program, input_count, toolchain); // your analysis
  if (!acceptable(facts)) throw new Error("rejected");     // throw to reject
  return [{ name: "pg_example_facts", source: renderBend(facts) }];
}
```

- `program` is the submitted source as `{path, content}` records, `contract` the
  plugin's frozen package, `input_count` the number of enrolled inputs, and
  `toolchain.source` the pinned Bend checkout, so you can import its parser.
- Each returned entry replaces the body of an existing plugin definition with the
  same name and type. Name such definitions with a `pg_` or `PG` prefix so the
  hidden renaming leaves them alone. Return `[]` when you have nothing to bind.
- The module must be self-contained: it is stored as text at enrollment, so
  relative imports do not resolve.

The verifier runs with the checker process's permissions, so the owner reviews it
like any other code they run.

## 4. Optional guard runner

Set `"entrypoint"` to a Bend function that takes `inputs` (and `owner` if the
selection has `owner_data`), calls `main(inputs)`, inspects the batch and returns
it unchanged or returns `PGAbort{}`. Use it for checks on concrete bytes.

## 5. Enroll

List the plugin in an enrollment configuration and run `proofguard/cli.py enroll`,
as in the [Quickstart](../quickstart.md#enroll-a-plugin). From Python:

```python
from pathlib import Path
from proofguard.plugin import Plugin

owner = Path("/absolute/owner")
owner.mkdir(mode=0o700, exist_ok=True)  # the CLI creates it; the API does not
selected = Plugin.load(Path("plugins/example")).enroll(
    owner, inputs_base64=[],
    session={"identifier": "example", "expires": 2000000000,
             "destinations": ["/absolute/out.txt"]})
```

Run it with `PYTHONPATH=/absolute/checkout:/absolute/checkout/bend-spec`.
`Plugin.enroll_all((first, second), …)` enrolls several plugins together.

## 6. Tests

Declare the test suite in `tests.json`:

```json
{
  "schema": 1,
  "name": "example",
  "tests": "plugins/example/tests",
  "sources": ["plugins/example"],
  "cases": {"blocks-other-destinations": "test_example.ExampleTests.test_blocks_other_destinations"}
}
```

`cases` maps your own case names to test IDs that must exist and pass; use `{}` if
you don't keep a case list. Run it after a passing foundation report:

```sh
python3 scripts/review-tests.py foundation
python3 scripts/review-tests.py plugin plugins/example/tests.json
```

The run fails on any failure, skip, missing case or changed source, and writes
`artifacts/review-stack/plugins/example.json`. CI runs every `plugins/*/tests.json`.
