# Limits

| Item | Limit |
| --- | --- |
| Plugins per enrollment | 16 |
| Plugin sources, bindings and verifier | 1 MiB per plugin |
| Verifier-derived definitions | 65,536 bytes each |
| Input snapshots | 16, of 65,536 bytes each |
| Owner data tree | 4 MiB + 4 KiB of bytes, 1,024 nodes, depth 128 |
| Enrollment record | 8 MiB, including all owner data |
| Configuration file | 16 MiB |
| Destinations | 16 |
| Writes per run | 16, across all steps |
| Bytes per write | 65,536 |
| Proposal size | 16,384 bytes of UTF-8 |
| Text literal (`pg_text`) | about 12 KB of ASCII in one literal (measured); join longer text with `pg_append` |
| Byte recursion (`pg_flatten`) | works up to 32 KB; at 60 KB the evaluator stops and nothing is written |
| Verification time | 60 seconds for all checks of one proposal |
| Execution time | 60 seconds per step, 300 seconds per run |
| HTTP endpoints | 16 |
| Calls per run | 8, shared with commands |
| Call time | 10 seconds, from name resolution to the last response byte |
| URL | 4,096 bytes of ASCII |
| Request headers set by the program | 16, values up to 1,024 bytes, 8,192 bytes in total |
| Request and response bodies | 65,536 bytes each; a larger response reaches the program as `PGHttpOversized` |
| Command templates | 16, each with up to 64 `argv` items, 16 slots and 32 `env` entries |
| Command argument | 4,096 bytes; `one_of` lists up to 64 values, `chars` up to 256 characters |
| Command time | The template's `timeout`, 1 to 60 seconds; a command does not start if the run has less time left |
| Command output | 65,536 bytes of standard output and error combined; more reaches the program as `PGRunOversized` |
| Credential file | 4,096 bytes, one printable ASCII header value |
| Outbox entries | 128, counting uploads and processed files. Above that the worker stops; the owner cleans or rotates the outbox. |
| Sandbox scratch space | `/tmp` 256 MiB, `/home` 64 MiB, `/run` 8 MiB |
| Brief for a harness session | 32 KiB |

Plugins and harnesses set their own limits: see [Meijer](../../plugins/meijer/README.md#limits)
and [pi](../../integrations/pi/README.md#limits).
