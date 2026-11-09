# Add a harness

A harness lives in its own directory under `integrations/`. Its non-test code
imports only `proofguard.session`, and it never imports a plugin; its tests and its
qualification runner (`review.py`) may use more of the foundation. Read
[Harnesses](../concepts/harnesses.md) first.

## Structure

A harness has two halves:

- **Guest code** runs inside the sandbox as the agent. It reads `/task`, writes
  proposals to `/outbox` and reads `/results`. It has no network.
- **Owner code** runs outside. It creates the `AuthoringSession`, starts the
  worker and the guest, and relays model traffic if the model is remote.

## Minimal owner code

```python
import os, shutil, subprocess
from pathlib import Path
from proofguard.session import AuthoringSession

GUEST = Path(__file__).resolve().parent / "guest"

def stage_runtime(destination: Path):
    shutil.copytree(GUEST, destination)

session = AuthoringSession(Path("/absolute/owner"), enrollment_id,
                           Path("/absolute/new-session"), brief,
                           stage_runtime=stage_runtime)
worker = subprocess.Popen(session.worker_command(os.getpid()))
subprocess.run(session.agent_command(["/usr/bin/node", "/runtime/agent.mjs"]))
worker.terminate()
worker.wait()

outcomes = session.outcomes()
session.publish_report({"outcomes": outcomes,
                        "reconcile": session.needs_reconciliation(outcomes)})
```

Run it with the checkout and `bend-spec/` on the import path, for example
`PYTHONPATH=/absolute/checkout:/absolute/checkout/bend-spec`. Start the worker
directly from the process whose ID you pass, not through a shell, or it exits at
once. `subprocess.run` closes inherited file descriptors by default; keep it that way.

If the harness holds credentials for a model provider, call
`session.require_private(path)` on them first; it raises if the path is visible
inside the sandbox.

## Submitting from the guest

1. Write the program to a hidden file in `/outbox`, for example `/outbox/.upload`.
2. Rename it to `/outbox/<32 random hex digits>.bend`.
3. Wait for `/results/<id>.json` and give its `status`, `reason` and
   `diagnostics` back to the model.

A rejected or completed proposal can be followed by a corrected one under a new ID.
Stop if a result is `uncertain`: the owner has to reconcile the files first.

## Model access

The sandbox has no network. To use a remote model, the guest sends inference
requests to the owner process (pi uses JSON lines on stdin and stdout), and the
owner forwards them with credentials the guest never sees. See the
[pi relay](../../integrations/pi/README.md#live-models).

## Tests

Keep tests in `integrations/<name>/tests/`. Tests that need a specific plugin go in
`tests/composition/`.
