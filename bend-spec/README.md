# Toolchain

`bend-spec/` connects ProofGuard to Bend. It checks source with the Bend checker
and the BendTT kernel, and evaluates accepted programs into the fixed IO frame.
Both steps use the same pinned toolchain.

## Setup

The tested platform is Linux x86-64 with Python 3.12, Node 22.23.4, Lean 4.34.0
and clang 18; the [quickstart](../docs/quickstart.md#install-the-toolchain) shows
where to get them. From the repository root:

```sh
python3 bend-spec/scripts/setup-toolchain.py --lean-home /path/to/lean-4.34.0-linux
```

Node is taken from `/usr/bin/node` and clang from `/usr/bin/clang`; `--node` and
`--clang` choose other binaries for the build, but the sandbox, evaluator and tests
still run `/usr/bin/node`. Setup refuses to start with less than 1,100 MiB free disk, though the build
itself adds only a few megabytes.

Setup downloads the Bend revision pinned in `toolchain.lock.json`, checks each
file's SHA-256, builds the BendTT kernel and records the runtime identities. Files
are cached in `bend-spec/.cache/` and reused when their digests match. Add
`--offline` to build from a preseeded cache without network access; a missing,
changed or symlinked file then fails setup.

Checking a program uses the installed toolchain and never downloads anything.
Every enrollment records the toolchain identity, and the gateway refuses to run
under a different one. The identity includes the kernel binary's hash, which depends
on where Lean is installed, so each machine has its own identity.

## Checking

The checker parses the plugin sources and the agent's program together, fills in
the agent's `main`, and proves each listed law automatically by reflexivity: the
law holds when both sides reduce to the same term. Both the Bend checker and the
BendTT kernel must accept. Holes, unsafe or foreign definitions, redefinitions of
protected declarations, timeouts and laws that need any other proof reject.

## Evaluation

After acceptance, the evaluator runs the entrypoint bound at admission with the
private inputs built as typed values. It returns a bounded JSON frame of
destination indexes and base64 bytes, which `proofguard/effects.py` validates.
It performs no file writes; the gateway does.

## Tests

```sh
PYTHONPATH=bend-spec:. python3 -m unittest discover -s bend-spec/tests -v
```

These also run as part of `python3 scripts/review-tests.py foundation`.
