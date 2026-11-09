# Security

ProofGuard is a security boundary, so reports about it are welcome.

## Reporting a vulnerability

Report privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Please do not open a public issue for a vulnerability.
Include the commit or release, the setup (enrollment, plugins, harness) and a
program or steps that reproduce it.

## Supported versions

Fixes go into the latest release.

## In scope

Anything that breaks what the [security model](docs/concepts/security-model.md)
says is enforced, for example:

- a program running or causing an effect without every enrolled check accepting it,
  or running different text from the text that was checked;
- an agent reaching the network, the owner's directory, private inputs, destinations
  or credentials from the sandbox;
- an effect outside the enrolled destinations or limits, or after expiry;
- plugin sources, law names, inputs or owner data reaching the agent;
- a guard runner changing a batch instead of only vetoing it;
- a Meijer program whose writes break the owner's labels and are still accepted.

## Known limits

These are documented and not treated as vulnerabilities on their own:

- the trusted components listed in the [security model](docs/concepts/security-model.md#what-is-trusted);
- result statuses that depend on private data through foundation limits, which
  Meijer's `outcome_safe` does not cover (see
  [Meijer's limits](plugins/meijer/README.md#limits));
- the outbox having no size limit, and batches not being transactional.

A practical way to exploit one of these beyond what the docs describe is still worth
reporting.
