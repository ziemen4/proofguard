# CI and releases

`.github/workflows/foundation.yml` runs on pull requests, pushes to `main` and
manual dispatch, on GitHub-hosted Ubuntu 24.04.

1. `scripts/ci/provision-toolchain.sh` downloads Lean 4.34.0 and Node 22.23.4,
   checks them against committed SHA-256 digests and builds the BendTT kernel. The
   cache holds only the verified archives; the kernel is rebuilt every run.
2. It installs that Node at `/usr/bin/node`, where the sandbox and some tests
   expect it, and runs the unit tests of the CI scripts in `scripts/ci/`.
3. `scripts/ci/check-foundation.py` confirms that `bwrap` can create a sandbox,
   then runs `tests/core` and `bend-spec/tests`.
4. `check-foundation.py --plugins` runs every `plugins/*/tests.json` profile.
5. The pi harness installs its locked dependencies and runs `integrations/pi/review.py`
   with a scripted model. A new harness's tests run in CI only once a step for them
   is added to the workflow.

A run fails on any failed, skipped or missing test, an empty suite, or a source or
toolchain change between steps. The foundation and plugin suites have 30-minute
deadlines and the pi suites 15 minutes. Logs and the
reports in `artifacts/review-stack/` are uploaded as an artifact for 14 days.

The job has read-only repository access, uses no secrets and pins every action by
commit. To give `bwrap` user namespaces, it sets
`kernel.apparmor_restrict_unprivileged_userns=0` on the disposable VM.

## Releases

`VERSION` holds the version, currently `0.1.0`. To release:

1. Update `VERSION` in a pull request and merge it.
2. Publish a GitHub release whose tag is `v` plus that version, such as `v0.1.0`.

`.github/workflows/docs.yml` builds the docs on pushes to `main` and on pull
requests. When a release is published it checks that the tag matches `VERSION`, then
`site/scripts/build-releases.mjs` builds the docs of every release tag and deploys
them to GitHub Pages: the newest at the root, each release under `/vX.Y.Z/`, with a
version switcher in the header. A manual run of the workflow redeploys the same
release docs. Deployment runs only when the repository variable `DOCS_DEPLOY` is
`true`; a Pages site is public even for a private repository.

To check the CI scripts locally:

```sh
python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v
```
