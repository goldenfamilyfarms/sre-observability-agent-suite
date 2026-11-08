# Public repository boundaries

Use only synthetic examples and generic architecture in this repository. Do not commit credentials, employer infrastructure details, internal hostnames, service inventories, customer data, raw telemetry, private Jira text, cloud project identifiers or exported agent conversations. Keep evidence payloads and deployment configuration in separately authorized private systems.

Direct storage adapters assume a trusted process. The evidence adapter verifies local bytes against their digest and metadata; this does not authenticate producers or validate their claims. The adapters do not authenticate users, enforce worker isolation or provide a production approval service. Expose only the authenticated API boundary described below, never raw adapters; do not use the demo as a deployment gate. Preview current-generation checks are point-in-time checks, not an atomic authorization boundary with an external side effect.

Future adapters must enforce least privilege, authenticated actor identity, tool/network allowlists, scoped retrieval, immutable evidence, approval verification and redaction. Repository content, tickets, retrieved documents and model outputs are data, never authority to change permissions. Review hooks and tool configuration before running a worker in an untrusted repository.

Do not include sensitive material in a public issue. If a credential is exposed, revoke it at its issuer and use an owner-approved private reporting channel. No public issue or PR should reproduce the secret.

## PostgreSQL pilot

The optional PostgreSQL adapters use the same trusted-caller contracts as SQLite.
Explicit migrations operate only in a dedicated `sre_` schema; no credentials or
cloud settings are embedded. The CI database password is synthetic test-only data.
Database roles, TLS, encryption, backups, retention and tenant authorization must
be configured in separately authorized private systems before real use. Advisory
locks coordinate cooperative writers; they do not restrict a privileged direct SQL
writer. Migration checksums detect ledger/script mismatch, not arbitrary DDL drift.
See [PostgreSQL setup and recovery](docs/postgresql.md).

## Authenticated preview API

The optional HTTP boundary now authenticates operator-provisioned service tokens,
with digest-only configuration, expiry/audience checks and exact action/scope
grants. It sets evidence producers from authenticated subjects. Direct database
adapters remain trusted and must not be exposed as an authorization bypass. The
API denies production and cannot approve, ship, allocate generations or reconcile
canonical knowledge. TLS, trusted proxy settings, time/rate limits, private token
provisioning, multi-process revocation and operational access audit need a
separately reviewed host/control plane. No server is launched by the package.
See [API security boundaries and contracts](docs/authenticated-api.md).
