# Phase 1, increment 3: authenticated evidence and scoped context API

Package 0.4.0 adds a WSGI application with authenticated evidence upload/read and canonical context retrieval. SQLite and PostgreSQL share the same API/domain checks. No listener, credentials, cloud resources or deployment are created by importing the application. Scope envelopes remain version 2; PostgreSQL migrations remain 1 and 2.

## Authentication and authorization

This pilot uses operator-provisioned opaque service credentials. Generate each token from at least 32 cryptographically random bytes (for example, `secrets.token_urlsafe(32)`), deliver it privately, and configure only its SHA-256 digest. Syntax validation does not prove entropy; do not use passwords or example tokens. The authenticator checks the bearer digest, audience, activation and expiry on every request. Authentication identifies the configured service subject possessing the credential; it is not human identity verification or proof that an observation is true.

Each credential contains an immutable set of `Grant(scope, action)` entries. Supported actions are `evidence:write`, `evidence:read` and `context:read`. A grant must match **every** Scope field: service, environment class, full revision, environment ID and generation. There are no wildcards, implicit roles or administrator headers. A new preview generation requires a newly authorized exact-scope grant. Credentials authorize only the listed actions; the same scope may be shared explicitly by several workers. Production is denied by this preview API even if someone configures a production grant. Existing production human-approval rules remain separate and unchanged.

Only `Authorization: Bearer <token>` is accepted. Cookies, query tokens and caller-provided identity/role headers are not authentication. Requests require HTTPS as reported by the trusted WSGI host; the application ignores forwarded-protocol headers. A TLS-terminating proxy must be separately configured to strip spoofable forwarding headers, set trusted connection metadata and protect the backend connection. Bearer-header and transport choices follow [RFC 6750](https://datatracker.ietf.org/doc/html/rfc6750); this is **not** an OAuth authorization server or OIDC/JWT validator.

`TokenAuthenticator.replace_credentials(tuple)` atomically replaces one process's registry. Remove a digest to revoke it; temporarily retain old and new digests for controlled rotation. Reload every serving process. This is local revocation, not distributed introspection. The API reauthenticates after acquiring the database lock and again before committing, so an observed revocation, scope-grant removal or expiry rolls back an in-flight upload. Revocation after the final check cannot retract an already committed request. A future identity-provider adapter must preserve these subject/action/scope/time checks.

## Configure the application explicitly

```python
import os
from datetime import UTC, datetime, timedelta
from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import postgres_backend
from sre_suite.auth import Action, Credential, Grant, TokenAuthenticator
from sre_suite.contracts import Environment, Scope

# Synthetic scope only. A trusted controller supplies authorized real scopes.
scope = Scope('demo-service', Environment.DEVELOPMENT, 'a' * 40, 'preview-r1-pr1', 1)
now = datetime.now(UTC)
credential = Credential(
    subject='engineering-worker-1',
    token_sha256=os.environ['SRE_TOKEN_SHA256'],  # Digest, never the bearer secret.
    audience='sre-evidence-pilot',
    not_before=now,
    expires_at=now + timedelta(minutes=30),
    grants=frozenset(Grant(scope, action) for action in Action),
)
auth = TokenAuthenticator('sre-evidence-pilot', (credential,))
application = EvidenceContextApi(
    auth, postgres_backend(os.environ['SRE_POSTGRES_DSN'], schema='sre_suite')
)
```

Run explicit PostgreSQL migrations and register an authorized current `PreviewRequest` before accepting requests. The API cannot allocate generations, create previews, issue credentials or approve releases. It opens/closes a fresh connection for each authorized operation; it never runs PostgreSQL migrations. For offline local tests, `sqlite_backend(path)` explicitly prepares the SQLite tables at application construction. File databases are required; independent `:memory:` connections do not share state.

Supply `application` to a separately reviewed HTTPS-capable WSGI host. No production server/configuration is included. Python's [wsgiref reference server](https://docs.python.org/3/library/wsgiref.html) is used only in tests. Configure host-level header/body/time limits, connection limits, rate limiting, private credential distribution and safe operational logging before exposure. Never log Authorization, request/response bodies or DSNs. The library returns generic backend errors and emits no request logs. Durable access audit, distributed credential management and workload federation remain future work.

## Endpoint contracts

All three endpoints use POST with `Content-Type: application/json` (optionally `charset=utf-8`) and a valid Content-Length. Query parameters, content encoding and transfer encoding are rejected by the application. The WSGI server must reject ambiguous/duplicate HTTP framing before constructing the environment. Every JSON object has an exact field set; duplicate keys, non-finite numbers, unsupported versions and coerced generation/version types are rejected. No browser CORS policy is enabled.

| Endpoint | Permission | Request | Successful response |
| --- | --- | --- | --- |
| `/v1/evidence` | `evidence:write` | `schema_version`, `evidence` without producer, `content_base64` | `schema_version`, stored `evidence` including server-owned producer and ID |
| `/v1/evidence/read` | `evidence:read` | `schema_version`, complete stored `evidence` | `schema_version`, `evidence`, `content_base64` |
| `/v1/context` | `context:read` | `schema_version`, `scope`, `expected_version` | `schema_version`, `scope`, global knowledge `version`, verified `findings` |

All successes return 200, including an identical upload retry. Supply `schema_version: 2`. The existing [scope contract](phase-1-foundation.md#identity-contracts) is unchanged.

### Upload

```json
{
  "schema_version": 2,
  "evidence": {
    "id": "observation-1",
    "scope": {
      "service": "demo-service",
      "environment": "development",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "environment_id": "preview-r1-pr1",
      "generation": 1
    },
    "uri": "urn:synthetic:observation-1",
    "sha256": "<64 lowercase hexadecimal characters matching the decoded bytes>",
    "observed_at": "2026-11-07T18:00:00+00:00",
    "expires_at": "2026-11-07T18:30:00+00:00"
  },
  "content_base64": "<canonical standard base64>"
}
```

The digest/content placeholders must be replaced with matching values and the timestamps must be current; the illustrative object is not an executable fixture. `id` is a caller-local key of at most 128 ASCII letters/digits/dots/underscores/hyphens, beginning with a letter or digit. The server generates the stored ID as `ev-` plus SHA-256 of a JSON tuple containing authenticated subject, encoded full scope and caller-local ID. Keep the returned metadata for later reads. The same local ID in another principal or generation has a different stored ID. Reusing one stored ID with different metadata/bytes is a conflict.

Upload requests must omit `producer`; the server sets it from the authenticated subject. Providing this field is rejected rather than trusted. The server checks the bytes against the digest, time window and immutable metadata; HTTPS evidence URIs cannot contain credentials, query strings or fragments. URIs are metadata only and are never fetched. API uploads prove possession of the configured service credential and byte integrity. Existing direct database writers remain trusted and can create records outside this boundary; their producer strings do not retroactively become authenticated.

### Read and context

For `/v1/evidence/read`, send `{ "schema_version": 2, "evidence": <exact returned evidence> }`. Full metadata, producer, scope, digest and expiry are verified before bytes are returned. Unknown, changed, expired or tampered evidence produces the same unavailable response after authorization. There is no list-by-ID or cross-scope search endpoint.

For `/v1/context`, send `{ "schema_version": 2, "scope": <full scope>, "expected_version": null }` for the current snapshot. An explicit nonnegative integer version requires that exact global knowledge version or returns a conflict. Only verified findings from that scope with currently verifiable evidence appear; proposed, disputed, differently scoped, expired or tampered records are excluded. The API also rejects a snapshot whose evidence expires during the request. A context is a point-in-time observation, not an execution/deployment authorization. Workers must continue to revalidate their context before completion.

No canonical write/reconciliation endpoint is exposed. Existing trusted reconciliation code supplies canonical facts; governed steward authorization and supersession remain future work. This increment adds an HTTP API, not an MCP server or Jira execution endpoint.

## Atomic generation checks and limits

For every database operation, the API holds the existing schema write lock while checking current preview state, verifying/reading evidence, obtaining context or writing an upload. All repositories share the same connection. Nested operations use savepoints and cannot independently commit the request. Scope validity and credential authority are checked again before commit. Concurrent cooperative generation updates must wait; after a generation changes, old scoped uploads, reads and context queries fail. Closed, missing and expired previews also fail. These are database transaction guarantees, not a fence with external cloud/GitOps side effects. Direct SQL writers can bypass cooperative locks and must be controlled with database permissions.

Limits: 1 MiB decoded evidence, 1.5 MB request, 2 MB response, 200 context findings, 16 JSON nesting levels and 10,000 decoded nodes. Metadata strings and credential sets also have fixed bounds. These are request/result limits, not a resource-isolation or throughput guarantee: context still scans verified findings and API reads serialize with writes in this pilot. Indexed scoped retrieval, pagination, connection pooling and quotas remain future scale work.

Responses include `Cache-Control: no-store`, `Pragma: no-cache` and `X-Content-Type-Options: nosniff`. Errors contain only an `error` code; no exception details, credentials or database diagnostics are returned.

| Status | Meaning |
| --- | --- |
| 400 | HTTPS required, malformed/unknown fields, bad encoding or unsupported query/framing |
| 401 | Missing, malformed, unknown, inactive, expired or revoked credential; includes Bearer challenge |
| 403 | Exact action/scope not granted, or production requested |
| 404 | Unknown route or unavailable evidence after scope authorization |
| 405 | Method other than POST |
| 409 | Preview no longer current, context version changed, or conflicting/invalid/expired evidence |
| 411 / 413 / 415 | Required length missing, size limit exceeded, or unsupported media type/encoding |
| 503 | Storage/unexpected failure, with generic response; reconcile uncertain writes and retry identical input |

## Validation

The default suite includes 28 new API/authentication/savepoint tests and all 70 prior tests. The PostgreSQL suite adds seven API tests to the prior 21: real HTTPS with generated temporary credentials/certificate, concurrent idempotent upload, a generation writer observed waiting on the actual PostgreSQL advisory lock, authenticated context after reopen, rollback on revocation/backend failure/process exit, and nested savepoint rollback. The HTTPS test creates a loopback-only listener and removes its temporary key/certificate; it performs no deployment. Integration tests require the `openssl` command and the existing disposable PostgreSQL test configuration.
