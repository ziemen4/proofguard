# Private data

The agent writes its program without seeing the owner's data. Private bytes reach
the program only after it is accepted, as runtime arguments.

## What the agent knows

The agent gets the foundation interface and the owner's brief. The brief describes
the inputs, for example "input 0 is a CSV of orders, input 1 is a customer list",
and the destinations by index. The agent writes code that processes the data it
was told about; it never receives the bytes. Anything that depends on the contents,
such as filtering rows, happens inside the Bend program at run time.

The agent does read each proposal's result. Whether a program completed or was
rejected can depend on the private inputs, so a plugin that keeps data from the
agent must constrain outcomes as well as writes. Meijer's `outcome_safe` law does this
for failures its own checks cause, but not for failures from foundation limits such
as the write size limit; see [Meijer's limits](../../plugins/meijer/README.md#limits).

## How data reaches the program

```text
Enrollment
  inputs_base64 ──> PGInputs ─────────> main(inputs)
  owner_data    ──> PGOwnerContext ───> guard(inputs, owner)
```

**Inputs.** Up to 16 snapshots of up to 65,536 bytes each. The evaluator builds a
`PGInputs` value directly from the bytes and passes it to `main`; the bytes are never
parsed as source.

**Owner data.** Each plugin selection can carry an `owner_data` tree for its guard
runner. The tree has three node types:

| Bend constructor | Enrollment JSON |
| --- | --- |
| `PGEmpty` | `null` |
| `PGBlob(bytes)` | `{"bytes_base64": "…"}` |
| `PGPair(left, right)` | `{"pair": [left, right]}` |

The foundation checks only this structure; the plugin's Bend code decides what the
records mean. Meijer stores exact-payload approvals this way. Supplying `owner_data`,
even `null`, gives the guard the signature `PGInputs -> PGOwnerContext -> PGIO`;
omitting it keeps `PGInputs -> PGIO`.

Neither inputs nor owner data appear in the public task or in check receipts. Both
are bound into the enrollment identity, and the owner data tree has a binary
encoding (PGV1) for that commitment, described in
[Configuration](../reference/configuration.md#owner-data).
