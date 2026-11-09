# Security model

## What is enforced

- The agent can only publish Bend source. It has no network, no execution tool and
  no access to the owner's directory, inputs, destinations or credentials.
- Nothing runs until every enrolled plugin and the foundation interface check
  accept the submitted text. The same text, with the same toolchain, is what runs
  (a typed `main` header is normalized first, before both).
- Programs write only to the destinations fixed at enrollment, by index, within the
  [limits](../reference/limits.md), and only before the session expires.
- Guard runners can veto a batch but cannot change it.
- Plugin rejections reach the agent as a generic status. Plugin sources, law names,
  inputs and owner data never enter the sandbox.
- Every proposal that reaches verification is recorded in the owner's audit log,
  with its source, check, execution attempt, planned writes and outcome.
- `tests/core/` runs these properties against real sandboxes, the real checker and
  real files, including hostile code inside the sandbox.

## What is trusted

The host operating system, `bwrap`, the pinned Bend toolchain (parser, BendTT
translation and kernel, evaluator), the IO codec and the gateway are trusted. So is
any plugin verifier: BendTT checks the laws over its facts, not the verifier's code.

## Behavior to plan for

- Each file write is atomic, but a batch is not. If anything fails after the first
  destination was replaced, the result is `uncertain` and the owner checks the files.
- The outbox has no size limit; the agent can fill the filesystem it lives on.
- The agent sees every outcome, so whether a program succeeds can reveal something
  about private inputs. Meijer constrains outcomes its own checks cause; a program
  can still trigger a foundation limit, such as the write size limit, depending on
  private data. See [Meijer's limits](../../plugins/meijer/README.md#limits).
- A new proposal ID is a new request and can repeat an earlier effect.
