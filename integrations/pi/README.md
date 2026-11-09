# pi

This harness runs the [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
agent SDK (0.85.1) inside the foundation's sandbox. The model gets two tools:

| Tool | What it does |
| --- | --- |
| `read` | Reads a regular file under `/task` or `/results`, up to 65,536 bytes. |
| `write_program` | Publishes up to 16,384 bytes of Bend source as a new proposal and returns its result. |

`write_program` writes the source to a hidden file, renames it to a random
`.bend` ID, waits for the matching result and checks that it belongs to that
proposal and source. After a completed or rejected result the model can submit
again. An uncertain or missing result, or a cancellation, ends the conversation
so the owner can check the files.

## Running a session

Install the locked dependencies once:

```sh
npm ci --ignore-scripts --prefix integrations/pi
```

With a scripted model, for tests and demos:

```sh
python3 -I integrations/pi/run-session.py \
  --authority /absolute/owner --enrollment ENROLLMENT_ID \
  --root /absolute/new-session --brief /absolute/brief.txt \
  --scenario /absolute/scenario.json
```

`--root` must not exist yet; its parent must. `--brief` is a text file with the
owner's instructions for this session, staged as `/task/brief.txt`.

A scenario lists the model's turns; each turn has either `text` or `toolCalls`:

```json
{"schema": 1, "turns": [
  {"toolCalls": [{"name": "read", "arguments": {"path": "/task/brief.txt"}}]},
  {"toolCalls": [{"name": "write_program", "arguments": {"program": "def main(inputs):\n  pg_write(PGZero{}, pg_input(PGZero{}, inputs))\n"}}]},
  {"text": "Finished."}
]}
```

## Live models

Replace `--scenario` with `--auth-dir AUTH_DIR --provider PROVIDER --model MODEL_ID`;
all three are required. The sandboxed guest has no network: it sends each inference
request to the owner process, which forwards it through `provider-relay.mjs` using
the credentials in `AUTH_DIR/auth.json`. The relay forwards no guest
headers, URLs or options, never runs tools and does not retry. `AUTH_DIR` must be
outside every sandbox mount; the launcher checks this. Live runs send the prompt
context to the provider and may cost money.

## What the owner gets

The launcher writes `trace.jsonl` and `report.json` in the session root, outside
the sandbox, prints a short summary and exits with status 1 unless `sdk_completed`.
The trace has every SDK event, tool call and result, with credentials redacted. The
report has the model's final `answer`, `guest_exit_code`, `sdk_completed` (the
dialogue finished and the guest exited cleanly), the gateway outcome of every
proposal (`gateway_outcomes`) and `reconciliation_required`. A final answer is only the model's claim; the gateway
outcomes and the destination files show what happened.

## How it is built

| File | Side | Role |
| --- | --- | --- |
| `owner.py`, `run-session.py` | owner | `PiSession`, built on `AuthoringSession`: starts the worker, guest and relay, supervises them and writes the report. |
| `provider-relay.mjs` | owner | Forwards inference to the provider. |
| `guest.mjs`, `session.mjs`, `sdk.mjs`, `config.mjs` | guest | Runs the pi SDK with the two tools and fixed limits. |
| `proofguard.mjs`, `outbox.mjs`, `files.mjs` | guest | Tool implementations. |
| `transport.mjs`, `trace.mjs` | both | JSON-lines protocol and tracing, used by the guest and the relay. |
| `scenario.mjs` | guest | Scripted model for offline runs. |

The guest prints one JSON object per line: `trace`, `inference` or `complete`.
The owner answers inference requests with `inference_event` or `inference_error`
on stdin, one request at a time, with frames up to 8 MiB.

## Limits

| Item | Limit |
| --- | --- |
| Session deadline | `--deadline`, 300 seconds by default, at most 600 |
| Model turns | 12 |
| Proposals per session | 24 |
| Wait for one result | 180 seconds |
| One inference request | 280 seconds |
| Scenario file | 256 KiB, 12 turns, 8 tool calls per turn |
| Result file read by `write_program` | 128 KiB; diagnostics up to 12,000 characters |

## Tests

```sh
python3 integrations/pi/review.py
```

This runs `integrations/pi/tests`, `tests/composition` and the Node tests with the
real sandbox, SDK and checker and a scripted model, and writes
`artifacts/review-stack/pi.json`.
