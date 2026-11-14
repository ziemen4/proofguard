# ProofGuard

ProofGuard separates planning from execution for AI agents. The agent plans by
writing a Bend2 program; it never acts directly. The owner's side checks that program
against the owner's rules, written as Bend laws, and only then executes it with data
and authority the agent never holds. Effects go through a fixed interface: writing enrolled
files and calling enrolled HTTP endpoints.

- **Foundation** (`proofguard/`, `bend-spec/`): the planning/execution split.
  Sandbox, outbox worker, Bend and BendTT verification, execution, effects and audit.
- **Plugins** (`plugins/`): the owner's rules. `plugins/meijer` controls data flows.
- **Harnesses** (`integrations/`): agent runtimes. `integrations/pi` runs the pi SDK.

Any plugin works with any harness; `tests/core/test_boundaries.py` keeps the three
parts independent.

## Documentation

Start with [docs/introduction.md](docs/introduction.md) and the
[quickstart](docs/quickstart.md). `site/` builds the same files into a searchable
documentation site, published for each release (see
[CI and releases](docs/reference/ci.md#releases)):

```sh
cd site && npm ci --ignore-scripts && npm run dev   # or: npm run build
```

## Tests

After [toolchain setup](docs/quickstart.md#install-the-toolchain), in this order (each
step requires the previous one to have passed):

```sh
python3 scripts/review-tests.py foundation
python3 scripts/review-tests.py plugin plugins/meijer/tests.json
npm ci --ignore-scripts --prefix integrations/pi
python3 integrations/pi/review.py
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described
in [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE).
