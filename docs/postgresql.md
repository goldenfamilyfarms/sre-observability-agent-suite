# Phase 1, increment 2: PostgreSQL persistence

Package 0.3.0 adds optional PostgreSQL persistence for evidence bytes, findings/graph edges, knowledge versions/events, preview requests/tombstones/events, and scoped checkpoints/idempotent operations. It preserves the v2 environment ID and generation contract and the existing SQLite interface. Phase 1 is still incomplete: distributed identity/access audit, a delivery outbox, Temporal evaluation, real Claude Code workers and live preview control remain future work.

## Install and explicit setup

```sh
python -m pip install '.[postgres]'
```

The extra pins Psycopg 3.3.3. The default installation still has no third-party runtime dependencies. Import `sre_suite.postgres` only when using this extra. PostgreSQL 16 and 17 are the integration-test targets.

```python
import os
from sre_suite.postgres import migrate, PostgresEvidenceStore, PostgresPreviewRegistry

# Supply private connection settings outside source control. Never print this value.
dsn = os.environ['SRE_POSTGRES_DSN']
schema = 'sre_suite'
# Deliberate operator action, before starting application writers:
applied_versions = migrate(dsn, schema)
evidence = PostgresEvidenceStore(dsn, schema)
previews = PostgresPreviewRegistry(dsn, schema)
try:
    # Existing EvidenceStore/PreviewRegistry operations and contracts apply.
    pass
finally:
    previews.close()
    evidence.close()
```

Use `PostgresKnowledgeStore(dsn, evidence, single_valued_predicates=frozenset(), schema=schema)` and `PostgresCheckpoints(dsn, scope, schema)` for the other stores. These implement the same operations as their SQLite counterparts and work with `PreviewCoordinator`. Use a separate instance per thread and close every adapter. Each instance owns one connection; pooling is not implemented. DSNs should explicitly select the intended database, identity and transport policy. No library code discovers cloud credentials or chooses a database automatically.

Schemas must match `sre_[a-z0-9_]{1,58}`. The runner creates a new dedicated schema; even an existing empty schema without its migration ledger is rejected. Neither `public` nor system schemas are accepted. Queries exclude `public` from their search path. Identifiers use Psycopg identifier composition and data uses bound parameters. Only trusted operators should have schema DDL access.

## Migrations and recovery

- `001_foundation.sql` creates all domain tables, version counters, audit events, references and size/state/version constraints. Scope envelopes retain contract version 2.
- `002_scope_guards.sql` adds checks for v2 scopes, valid environment IDs, positive signed-64-bit generations, matching finding/evidence scopes and matching preview repository/PR identity. It also adds state/event indexes. These checks supplement application validation; they do not authenticate callers or prove evidence truth.
- `schema_migrations` records version, filename, checksum and application time. Repeating migration is a no-op. Changed checksums, unknown versions and gaps fail closed. Editing an applied SQL file is unsupported; add a new ordered migration instead.
- All pending migrations and ledger updates run in one transaction under the same schema lock used by application writers. Failed SQL, invalid existing scope data or process termination rolls back uncommitted changes. Adapters refuse pending or incompatible history. The ledger detects script/history mismatch, not arbitrary manual DDL drift.

For an upgrade, stop writers, take and test a PostgreSQL backup, run migrations with an authorized migration identity, then restart the compatible application. Inspect failed data privately; do not rewrite identity or generations to make validation pass. There is no destructive down-migration or SQLite importer. Recovery is to fix the cause and retry an unapplied migration, or stop writers and restore the reviewed backup together with the matching application version. Preserve any intervening records before a restore. SQLite v1 quarantine remains a separate operation; historical unscoped evidence is never adopted automatically.

## Transaction and identity behavior

All repository writes in one schema take a transaction advisory lock before reading current state. This deliberately serializes writes, including new IDs that have no row to lock yet. It preserves generation comparison, immutable evidence IDs, knowledge-version CAS and operation retries across connections. Knowledge snapshots use a repeatable-read transaction, so version and findings refer to the same committed database snapshot. Evidence is checked through its own verifier connection; this is not an atomic cross-service snapshot or a promise against later tampering.

Preview IDs remain `preview-r<repository-id>-pr<PR-number>`. New generations advance by exactly one. Identical active requests are idempotent; altered, stale, closed and skipped requests are rejected. Closing a preview writes a tombstone and audit event; it does not delete cloud resources. Checkpoint reads, writes and replay remain bound to the full scope. A new generation uses a new run ID. Authorized non-production promotion and human production approval rules are unchanged.

Knowledge graph predicates remain multi-valued unless explicitly configured as single-valued. Global knowledge versions conservatively invalidate outstanding worker context after any reconciliation. Event tables are atomic audit records, not an outbox with claims, dispatch and acknowledgements. The coordinator checks are point-in-time checks; no cloud/GitOps operation is performed or fenced by this library.

Connections use bounded connect, lock, statement and idle-transaction timeouts. Lock/connection errors propagate; there is no hidden retry loop. A trusted controller must reconcile an uncertain commit and retry with the exact same request/idempotency key. Retrying under a new key can create a new operation. Larger migrations may need a separately reviewed timeout policy; this pilot uses 15-second statements and 5-second lock waits.

## Run real database tests

Use a disposable local database, never an existing application or production database. The credentials below are synthetic and only for the isolated test container.

```sh
docker run --detach --rm --name sre-suite-test-postgres \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_USER=sre_test --env POSTGRES_PASSWORD=synthetic-test-only \
  --env POSTGRES_DB=sre_test postgres:17
# Wait for readiness before the test command:
docker exec sre-suite-test-postgres pg_isready -U sre_test -d sre_test
SRE_TEST_POSTGRES_DSN='postgresql://sre_test:synthetic-test-only@127.0.0.1:55432/sre_test' \
  python -m unittest discover -s integration_tests -v
docker stop sre-suite-test-postgres
```

The integration suite fails if the driver, database or explicit DSN is missing; it never silently skips. Each test creates a unique `sre_test_` schema and drops only that schema afterward. CI supplies disposable PostgreSQL 16/Python 3.11 and PostgreSQL 17/Python 3.14 services. The ordinary `python -m unittest discover -v` remains offline. The later [authenticated API increment](authenticated-api.md) expands both suites and uses nested savepoints to share one generation-checked request transaction.

The 21 integration tests cover clean install, upgrade preservation, repeat migration, checksums/unknown history, unmanaged schemas, concurrent migration, failed-DDL rollback, SQL scope guards, reopen persistence, evidence tampering, graph semantics, cross-preview/generation checkpoints, simultaneous absent-row inserts and CAS races, consistent snapshots, stale worker completion, failed checkpoint audit writes and subprocess exit during a preview update. A successful database test is not a deployment, production recovery exercise or service certification.
