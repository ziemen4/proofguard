# Configuration

## Enrollment

`proofguard/cli.py enroll --config FILE` reads:

```json
{
  "schema": 1,
  "plugins": [{"path": "examples/foundation/report"}],
  "inputs_base64": ["…"],
  "session": {
    "identifier": "reports",
    "expires": 2000000000,
    "destinations": ["/absolute/public.txt", "/absolute/private.txt"],
    "endpoints": [
      {"origin": "https://api.example.com", "path_prefix": "/v1/", "methods": ["GET", "POST"],
       "credential": {"header": "Authorization", "file": "/absolute/api-token"}}
    ],
    "commands": [
      {"argv": ["/usr/bin/dig", "+short", {"slot": 0}, {"slot": 1}],
       "slots": [{"chars": "a-z0-9.-", "max_bytes": 253}, {"one_of": ["A", "AAAA", "MX"]}], "timeout": 5}
    ]
  }
}
```

A plugin with bindings and a guard that takes owner data is selected like this:

```json
{"path": "plugins/example",
 "bindings": [{"path": "settings.bend", "content": "…Bend source…"}],
 "owner_data": {"bytes_base64": "…"}}
```

| Field | Meaning |
| --- | --- |
| `plugins[].path` | Plugin directory, relative to the configuration file or absolute. |
| `plugins[].bindings` | `{path, content}` for every path in the manifest's `bindings`, in the same order. |
| `plugins[].owner_data` | Private tree for the plugin's guard. Present (even `null`) when the guard takes `PGOwnerContext`; omitted when it takes only `inputs`. See [Owner data](#owner-data). |
| `inputs_base64` | Private input snapshots; `main` reads them by index. |
| `session.identifier` | Names the session's state and audit directory. Enrollments that reuse it share proposal claims and audit. |
| `session.expires` | Unix time in seconds. Checked before execution and before each write. |
| `session.destinations` | Absolute file paths; programs write to them by index. Parent directories must exist. |
| `session.endpoints` | Optional HTTP endpoints; programs call them by index. See [HTTP endpoints](#http-endpoints). |
| `session.commands` | Optional command templates; programs run them by index. See [Commands](#commands). |

Enrollment prints `{"schema": 1, "enrollment_id": "…", "contract_id": "…"}`.

## HTTP endpoints

| Field | Meaning |
| --- | --- |
| `origin` | `http` or `https`, host and optional port. A call's URL must have exactly this origin. |
| `path_prefix` | The URL path must start with it. Paths with `.` or `..` segments, encoded `/`, `\` or `%`, or control bytes are refused. |
| `methods` | Any of `GET`, `POST`, `PUT`, `PATCH`, `DELETE`. |
| `credential` | Optional `{header, file}`. At each call the gateway reads the file and adds its contents as that header. The file stays outside every sandbox mount. |
| `private_network` | Optional, default `false`. `true` allows loopback and private addresses, for local services. |

Endpoints must be distinct and are part of the enrollment, so changing one needs a
new enrollment. The credential file's contents are not: the owner can replace them,
for example to rotate a short-lived token, without enrolling again.

## Commands

| Field | Meaning |
| --- | --- |
| `argv` | The program's absolute path, then its arguments. Each item is a fixed string or `{"slot": n}`; every slot fills exactly one item. |
| `slots` | Optional. Each is `{"one_of": [...]}`, a list of exact values, or `{"chars": "a-z0-9._-", "max_bytes": n}`: 1 to `n` bytes from that set, not starting with `-`. In `chars`, `x-y` is a range and a `-` at either end is literal; only printable ASCII is allowed. |
| `timeout` | Optional, 1 to 60 seconds, default 10. |
| `cwd` | Optional absolute working directory, default `/`. |
| `env` | Optional map of environment variables; the command gets these and nothing else. |

The command runs as the worker's user. `env` is stored in the enrollment record and
a command can print it into output the program reads, so keep secrets out of it.

Enrollment refuses a template whose program is a write destination. For programs
that run their arguments as code (shells, interpreters, `env`, `xargs`, `git`,
`find` and wrappers such as `nice` or `ionice`) it also refuses `chars` slots and
any argument that names a write destination. The list cannot cover every program,
so review what each template can reach.

## Plugin manifest

`plugin.json` in the plugin directory:

| Field | Meaning |
| --- | --- |
| `schema` | `1`. |
| `name`, `intent` | Name and a one-line description. |
| `sources` | Bend files, in declaration order. |
| `bindings` | Subset of `sources` whose contents the owner supplies at enrollment. |
| `laws` | Laws that must hold for every submission. |
| `entrypoint` | `main`, or the name of a guard runner. |
| `checker` | Optional path to a JavaScript verifier. |

Sources, bindings and the verifier share a 1 MiB budget per plugin. A plugin declares at
least one law, and the same plugin cannot be selected twice.

For Meijer, generate the whole plugin entry (both bindings and `owner_data`) with
`MeijerPlugin(...).enrollment_config(session)`; see the [Meijer README](../../plugins/meijer/README.md#setup).

## Owner data

| Bend constructor | JSON | PGV1 encoding |
| --- | --- | --- |
| `PGEmpty` | `null` | `00` |
| `PGBlob(bytes)` | `{"bytes_base64": "…"}` | `01`, 4-byte big-endian length, bytes |
| `PGPair(left, right)` | `{"pair": [left, right]}` | `02`, left, right |

The PGV1 encoding starts with the ASCII header `PGV1`. Its digest (domain
`private-data-v1`) and the input digest are bound into the execution identity
(domain `private-execution-v1`), so any change to shape or bytes changes the enrollment.

## Plugin test profile

`tests.json`, run with `scripts/review-tests.py plugin`:

| Field | Meaning |
| --- | --- |
| `schema` | `1`. |
| `name` | Lowercase name used for the report file. |
| `tests` | unittest discovery directory, relative to the checkout. |
| `sources` | Non-empty list of files or directories to fingerprint, besides the profile and tests. |
| `cases` | Map from your case names to distinct test IDs that must run and pass; `{}` for none. |

## Command line

| Command | Purpose |
| --- | --- |
| `proofguard/cli.py enroll --authority DIR --config FILE` | Create an enrollment. |
| `proofguard/cli.py task --authority DIR --enrollment ID` | Print the public task. |
| `proofguard/cli.py watch --authority DIR --enrollment ID --outbox DIR --results DIR [--once]` | Process proposals. |
| `scripts/review-tests.py foundation` | Foundation and toolchain test suites. |
| `scripts/review-tests.py plugin PROFILE` | A plugin's suite. |
| `integrations/pi/review.py` | The pi harness suites. |

Run `proofguard/cli.py` with `python3 -I`. On failure it prints a generic message,
because owner configuration can contain private data.
