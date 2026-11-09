# Quickstart

This walks through enrolling the report example, submitting a program by hand
and reading the result. No agent or model is involved.

## Requirements

- Linux x86-64 with unprivileged user namespaces, so `bwrap` (bubblewrap) can create
  a sandbox. Ubuntu 24.04 restricts them through AppArmor; allow them with
  `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`.
- Python 3.12, clang 18, Lean 4.34.0 and Node 22.23.4.
- About 2 GB of free disk for the toolchain build.

On Ubuntu 24.04:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends python3 bubblewrap clang curl ca-certificates xz-utils zstd
```

## Install the toolchain

Download Lean and Node into a tools directory and check their SHA-256 digests:

```sh
mkdir -p ~/proofguard-tools && cd ~/proofguard-tools
curl -fLO https://github.com/leanprover/lean4/releases/download/v4.34.0/lean-4.34.0-linux.tar.zst
echo 'caaa98356098c85dc0fcbbd28e1ec66f39eb6551829972b752ff20e1286b646b  lean-4.34.0-linux.tar.zst' | sha256sum -c
curl -fLO https://nodejs.org/download/release/v22.23.4/node-v22.23.4-linux-x64.tar.xz
echo '1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce  node-v22.23.4-linux-x64.tar.xz' | sha256sum -c
tar -xf lean-4.34.0-linux.tar.zst && tar -xf node-v22.23.4-linux-x64.tar.xz
```

Node must be installed at `/usr/bin/node`. The sandbox only mounts `/usr`, and the
evaluator, the tests and the pi harness run that path:

```sh
sudo install -m 0755 node-v22.23.4-linux-x64/bin/node /usr/bin/node
```

`npm`, used for the pi harness and the docs site, comes in the same archive. Put it on
your `PATH`:

```sh
export PATH="$HOME/proofguard-tools/node-v22.23.4-linux-x64/bin:$PATH"
```

Do not install the distribution's `npm` or `nodejs` packages: they bring an older Node
and replace `/usr/bin/node`.

Then, from the checkout, build the checker. Setup downloads the Bend revision pinned in
`bend-spec/toolchain.lock.json`, checks every file's SHA-256 and builds the BendTT
kernel:

```sh
python3 bend-spec/scripts/setup-toolchain.py --lean-home ~/proofguard-tools/lean-4.34.0-linux
```

`scripts/ci/provision-toolchain.sh` does the same in CI. See
[Toolchain](../bend-spec/README.md) for offline setup and caching.

### In Docker

`bwrap` needs these flags on `docker run`, because Docker's default seccomp and
AppArmor profiles and its masked `/proc` paths block new namespaces:

```sh
docker run --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --security-opt systempaths=unconfined …
```

Run ProofGuard as a non-root user inside the container.

## Run the tests

Each step requires the previous one to have passed on the same checkout:

```sh
python3 scripts/review-tests.py foundation
python3 scripts/review-tests.py plugin plugins/meijer/tests.json
npm ci --ignore-scripts --prefix integrations/pi
python3 integrations/pi/review.py
```

They run real sandboxes and the real checker. On 8 cores the foundation and Meijer
suites take about 20 minutes each and pi about 3. Reports land in
`artifacts/review-stack/`.

## Enroll a plugin

The worker refuses to use directories inside the checkout, so work from a separate
directory. `CHECKOUT` is the absolute path of your clone:

```sh
CHECKOUT=/absolute/path/to/proofguard
mkdir -p ~/proofguard-demo/out && cd ~/proofguard-demo
```

The [report example](../examples/foundation/README.md) has one law: `main` must
copy input 0 to destination 0. Save this as `session.json`, with your own paths:

```json
{
  "schema": 1,
  "plugins": [{"path": "/absolute/path/to/proofguard/examples/foundation/report"}],
  "inputs_base64": ["aGVsbG8gZnJvbSB0aGUgb3duZXI="],
  "session": {
    "identifier": "quickstart",
    "expires": 2000000000,
    "destinations": ["/home/you/proofguard-demo/out/report.txt"]
  }
}
```

Enroll, then export the public task an agent would receive:

```sh
python3 -I "$CHECKOUT/proofguard/cli.py" enroll --authority "$PWD/owner" --config session.json
# {"schema": 1, "enrollment_id": "570db7…", "contract_id": "1ad672…"}
python3 -I "$CHECKOUT/proofguard/cli.py" task --authority "$PWD/owner" --enrollment ENROLLMENT_ID
```

The task contains the foundation interface and instructions. The plugin's law and
the input bytes stay in `owner/`.

## Submit a program

A proposal is a `.bend` file named with 32 random hex digits. Write it under a
hidden name first, then rename it, so the worker never sees a partial file:

```sh
mkdir -p outbox results
printf 'def main(inputs):\n  pg_write(PGZero{}, pg_input(PGZero{}, inputs))\n' > outbox/.upload
mv outbox/.upload "outbox/$(python3 -c 'import secrets; print(secrets.token_hex(16))').bend"
python3 -I "$CHECKOUT/proofguard/cli.py" watch --authority "$PWD/owner" \
  --enrollment ENROLLMENT_ID --outbox "$PWD/outbox" --results "$PWD/results" --once
```

The worker prints the result and publishes it to `results/<id>.json`:

```json
{"schema": 1, "proposal": "3ebaf4…", "status": "completed", "reason": null, …}
```

`out/report.txt` now contains `hello from the owner`. Submit
`def main(inputs):\n  PGDone{}\n` instead and the result is `"status": "rejected"`
with reason `mandatory_obligation_failed`, and nothing is written. Without `--once`
the worker keeps polling until the session expires.

To have a model write the program instead, run a [harness](../integrations/pi/README.md).
