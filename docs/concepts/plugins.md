# Plugins

A plugin adds the owner's rules to the execution side's checks. The rules are Bend
laws about what an agent's program may do. It is a directory with a `plugin.json` manifest and Bend sources. The
foundation loads it from the manifest; it never imports plugin code.

## A minimal plugin

The [report example](../../examples/foundation/README.md) has one source file and
one law:

```bend
# checks.bend: the owner's reference implementation and the law.
def write_report(inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}

law owner_report:
  {main == write_report : PGInputs -> PGIO}
```

```json
{
  "schema": 1,
  "name": "owner-report",
  "intent": "Publish the owner report",
  "sources": ["checks.bend"],
  "bindings": [],
  "laws": ["owner_report"],
  "entrypoint": "main"
}
```

A law refers to the agent's `main`, so it states a property of whatever program
the agent submits. Here `main` must equal `write_report` under Bend's definitional
equality, for every input, not just one test run. The checker proves each listed
law automatically: it accepts an equality when both sides reduce to the same term,
and rejects every other law.

## What a plugin can add

| Manifest field | What it does |
| --- | --- |
| `sources` | Bend files in declaration order. |
| `laws` | Laws that must hold for every submission. |
| `bindings` | Source files the owner supplies at enrollment, such as settings. They are checked like any other source. |
| `entrypoint` | A guard runner, when it is not `main`. |
| `checker` | A JavaScript verifier, for checks that need program analysis. |

**Verifier.** Some properties depend on the structure of the program, such as which
input can reach which destination. A plugin's verifier receives the submitted source
and returns Bend definitions describing it (facts); the plugin's laws then check
those facts. The verifier rejects by throwing. Facts exist only during
verification; the program that runs never sees them. [Meijer](../../plugins/meijer/README.md)
works this way.

**Guard runner.** Some checks need the actual bytes, such as an approval for an
exact payload. A guard runner is a Bend function that calls the agent's `main`,
inspects the resulting batch and returns it unchanged or returns `PGAbort`. The
foundation runs `main` separately and writes nothing unless every guard returns
the identical batch. A guard can veto a batch but never change it.

**Owner data.** A guard can also receive private records the owner supplies at
enrollment, as a `PGOwnerContext` argument. See [Private data](private-data.md).

## How plugins combine

An enrollment can select up to sixteen plugins. Each one checks the same submitted
source in its own context, and all must accept. The foundation renames every
plugin declaration with a hidden prefix when loading it, so an agent's helper can
never collide with a plugin name it cannot see. Names starting with `pg_` or `PG`
keep their spelling, so a verifier can refer to them.

The agent sees none of this. Its task contains only the foundation interface, and a
plugin rejection reports `mandatory_obligation_failed` without saying which law
failed. The details go to the owner's audit.

## Testing a plugin

A plugin keeps its tests next to its code and declares them in a `tests.json`
profile. `python3 scripts/review-tests.py plugin <profile>` runs them against a
passing foundation report. See [Write a plugin](../guides/write-a-plugin.md).
