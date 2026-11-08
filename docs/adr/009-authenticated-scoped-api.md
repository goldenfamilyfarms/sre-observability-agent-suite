# ADR-009: authenticated evidence and context boundary

Status: accepted for the Phase 1 preview pilot. OIDC/workload federation and production hosting are not selected by this decision.

## Decision

Expose evidence upload/read and canonical context retrieval through a small WSGI application. Retain the existing database/domain logic and version-2 scopes. Use operator-provisioned, high-entropy opaque bearer credentials, storing only their SHA-256 digests alongside subject, audience, validity window and exact-scope action grants. Reject missing or malformed authentication, ungranted actions/scopes, and all production requests. Do not accept client roles or producer identity.

Use the authenticated subject as upload producer and derive immutable evidence IDs from subject, complete scope and caller-local ID. This prevents one principal or generation from claiming another's evidence ID while retaining shared reads when explicitly authorized. Validate untrusted JSON strictly and bound inputs/responses. Return generic errors and disable response caching.

Hold one database transaction/generation lock for each operation and share its connection across preview, evidence and knowledge repositories. Nested repository transactions use savepoints. Recheck credential authority and preview validity after locking and before committing. This closes the database check/write race for cooperative generation updates without claiming a distributed deployment fence.

## Tradeoffs and boundaries

The existing stack has no identity provider, trusted proxy or service host configured. Opaque service credentials provide a testable authentication boundary without inventing issuer/JWK infrastructure or implementing JWT cryptography. They require private provisioning and operator reload in every process for rotation/revocation. Possession of the token represents its configured service subject; it is not human approval or truth verification. A future identity-provider adapter must retain exact grants and failure behavior.

WSGI keeps the standard-library core and allows a separately reviewed host. HTTPS is mandatory and forwarded headers are not trusted by the application. Tests exercise both WSGI conformance and an actual loopback TLS connection; the reference test server is not a production recommendation. Host hardening, distributed access audit, quotas, scoped indexed retrieval and a deployment remain separate work.

Production authorization is unchanged and unreachable from these routes. Canonical mutation, preview creation/generation allocation, shipping, credential issuance and arbitrary URI fetching are not API operations. See the [API contract and setup guide](../authenticated-api.md) for full behavior and limitations.
