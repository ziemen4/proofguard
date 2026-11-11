# Security model

## What is enforced

- The agent can only publish Bend source. It has no network, no execution tool and
  no access to the owner's directory, inputs, destinations or credentials.
- Nothing runs until every enrolled plugin and the foundation interface check
  accept the submitted text. The same text, with the same toolchain, is what runs
  (a typed `main` header is normalized first, before both).
- Programs write only to the destinations fixed at enrollment, by index, within the
  [limits](../reference/limits.md), and only before the session expires.
- Programs call only the HTTP endpoints fixed at enrollment. The gateway checks
  each concrete URL, method and header before sending, adds owner credentials
  itself and connects only to addresses it has checked.
- Guard runners can veto a step but cannot change it.
- Plugin rejections reach the agent as a generic status. Plugin sources, law names,
  inputs and owner data never enter the sandbox.
- Every proposal that reaches verification is recorded in the owner's audit log,
  with its source, check, execution attempt, planned writes, calls and outcome.
- `tests/core/` runs these properties against real sandboxes, the real checker and
  real files, including hostile code inside the sandbox.

## What is trusted

The host operating system, `bwrap`, the pinned Bend toolchain (parser, BendTT
translation and kernel, evaluator), the IO codec and the gateway are trusted, as are
the system's DNS resolver and TLS certificate store for HTTP calls. So is
any plugin verifier: BendTT checks the laws over its facts, not the verifier's code.

## Behavior to plan for

- Each file write is atomic, but a run is not. If anything fails after the first
  effect began, the result is `uncertain` and the owner checks the files and
  endpoints.
- A sent call cannot be undone, and a timeout after sending is `uncertain`: the
  server may have acted on it.
- Labels and laws can keep data from reaching an endpoint, but a response is still
  data from that server. A public response from an untrusted server can steer what
  the program does within the laws.
- The outbox has no size limit; the agent can fill the filesystem it lives on.
- The agent sees every outcome, so whether a program succeeds can reveal something
  about private inputs. Meijer constrains outcomes its own checks cause; a program
  can still trigger a foundation limit, such as the write size limit, depending on
  private data. See [Meijer's limits](../../plugins/meijer/README.md#limits).
- A new proposal ID is a new request and can repeat an earlier effect.
