# Harnesses

A harness is the planning side of ProofGuard: it runs an agent, such as a model
with tools, inside the foundation's sandbox. The harness decides how the agent works
and talks to its model. The agent's only output is programs; the foundation and the
plugins decide what those programs may do.

## The session module

A harness uses one foundation module, `proofguard/session.py`. Its
`AuthoringSession` sets up one authoring session from an existing enrollment:

```python
from proofguard.session import AuthoringSession

session = AuthoringSession(authority, enrollment_id, root, brief,
                           stage_runtime=copy_guest_code)
```

| Member | What it does |
| --- | --- |
| constructor | Loads the enrollment and builds its worker, writes `task.json`, `brief.txt` and any extra public `files` (plain names only) to `/task`, and calls `stage_runtime` to put the harness's guest code in `/runtime`. `root` must be a new directory. |
| `agent_command(argv)` | Wraps the harness's agent command in the sandbox. |
| `worker_command(parent)` | The owner worker command. Process `parent` must start it directly; the worker exits when `parent` dies. |
| `require_private(path)` | Refuses an owner path, such as a credentials directory, that the agent's mounts would expose. |
| `outcomes()` | The gateway's result for each proposal. |
| `needs_reconciliation(outcomes)` | True if an outcome is uncertain, or a proposal claimed from this session's outbox has no result. |
| `publish_report(report)` | Writes the session's final report atomically. |

The harness starts both processes, supervises them and carries its model traffic.
The enrollment ID is all it needs to know about the plugins, so any harness runs
with any set of plugins.

## What the agent gets

Inside the sandbox the agent can read `/task` and `/results` and write to
`/outbox`. A harness gives its model tools over those directories. To submit a
program, write the source to a hidden file in `/outbox`, then rename it to
`<32 hex digits>.bend`, and wait for `/results/<id>.json`.

Rules for writing programs come from the task, so a harness only describes its own
tools. The agent never gets an execution tool: the worker outside the sandbox
decides what runs.

## pi

[pi](../../integrations/pi/README.md) is the included harness. It runs the pi SDK
with two tools, `read` and `write_program`, and can use a scripted offline model
for tests or a live provider through an owner-side relay.
`integrations/pi/owner.py` shows a complete harness built on `AuthoringSession`.
