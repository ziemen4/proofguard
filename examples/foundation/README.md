# Report example

The smallest useful plugin: one law requiring the agent's `main` to copy input 0
to destination 0. It shows the full path from enrollment to execution.

| File | Role |
| --- | --- |
| [plugin.json](report/plugin.json) | Manifest: one source, one law. |
| [checks.bend](report/checks.bend) | The owner's `write_report` and the law `main == write_report`. |
| [program.bend](report/program.bend) | A program an agent might submit. Not part of the plugin. |

The law compares whole functions under Bend's definitional equality, so it covers
every input. A program that copies the input passes:

```bend
def main(inputs):
  pg_write(PGZero{}, pg_input(PGZero{}, inputs))
```

A program that returns `PGDone{}`, or writes anything else, is rejected with
`mandatory_obligation_failed` and nothing is written. The agent may still add its
own helpers, as long as `main` reduces to the same function.

The [Quickstart](../../docs/quickstart.md) enrolls this plugin and runs both cases.
`tests/core/test_plugin.py` tests it, including the CLI path.
