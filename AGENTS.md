# AGENTS.md - ProofGuard

## Repository

- `proofguard/`: generic enrollment, admission, worker, gateway and audit.
- `bend-spec/`: source-only checker integration and pinned toolchain setup.
- `proofguard/stdlib/`: fixed Bend primitive IO library.
- `proofguard/sandbox.py`: generic networkless Linux authoring sandbox.
- `proofguard/session.py`: the only foundation module harnesses use.
- `proofguard/plugin.py`: owner-side checks and enrollment configuration.
- `proofguard/cli.py`: owner enroll, task and watch commands.
- `plugins/meijer/`: law plugin and its tests.
- `examples/foundation/report/`: example plugin and agent-written Bend program.
- `tests/fixtures/selection/`: plugin fixture for lifecycle regression tests.
- `tests/core/`: real foundation integration tests.
- `integrations/pi/`: confined SDK authoring and its tests.
- `tests/composition/`: tests that need a specific plugin and harness together.
- `docs/`: documentation; `site/` builds it, with `site/pages.mjs` listing the pages.

Read [Foundation](docs/concepts/foundation.md) for the architecture and review order,
and [Security model](docs/concepts/security-model.md) for what is enforced and trusted.

## Invariants

- The agent submits Bend2 source, never owner configuration or native code.
- Owner enrollment fixes every Bend law package and guard entrypoint, private input
  context, destinations and expiry before agent requests are accepted.
- Check and execute the same captured source with the pinned toolchain.
- Require every enrolled plugin to accept. Run main in the foundation-only
  context; optional Bend guard runners may veto, never substitute its batch.
- Export only the fixed foundation interface; plugin sources stay owner-side.
- Keep mandatory check sources and detailed rejection diagnostics owner-side.
- Preview must not launch the application evaluator or resolve private inputs.
- Verify every submission, even after a successful preview or prior execution.
- Archive each execution attempt before application execution. A session can
  accept multiple proposals until expiry; never automatically retry a claimed ID.
- Only the gateway writes protected destinations. Packages contain Bend code;
  never restore a Python application callback or interpreter as a fallback.
- The host decodes only the fixed primitive IO result. Application computation
  and conditional behavior belong in the checked Bend program/library.
- Keep PGW1 binary authorization commitments distinct from the explicitly
  canonical JSON used for source identities and local archive records.
- Reject unsupported schemas and failed checks; no alternate execution path.
- Keep the owner pull worker outside the sandbox. Never mount owner authority
  or destinations inside it, or add direct execution RPCs to the agent tools.

## Modularity

The foundation, plugins and harnesses are separately replaceable. The foundation names
no plugin or harness, plugins and harnesses do not import each other, and harnesses use
only `proofguard/session.py` outside their tests and `review.py`.
`tests/core/test_boundaries.py` enforces this for Python and JavaScript imports. A plugin
or harness keeps its code, docs and tests in its own directory.

## Verification

Run the four steps in [README](README.md#tests) in order, with the real provisioned
toolchain: foundation, Meijer, the pi `npm ci`, then `integrations/pi/review.py`.
The tests exercise runtime behavior and actual Linux namespaces. Toolchain
refinement, kernel exploits and VM escapes remain outside their coverage.
