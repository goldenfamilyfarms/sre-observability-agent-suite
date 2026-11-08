# ADR-008: PostgreSQL persistence with shared guards and explicit migrations

Status: accepted for the Phase 1 persistence pilot. Supersedes the PostgreSQL-candidate portion of ADR-003; does not select the durable orchestrator or enable shipping.

## Context and decision

The SQLite foundation already enforces environment ID, generation, evidence integrity and optimistic checkpoint/knowledge updates. Duplicating that logic in a second database implementation risks diverging authorization and retry behavior.

Keep one implementation of the repository operations and a small internal SQL/transaction port. SQLite retains its existing constructor and explicit legacy-quarantine behavior. Optional PostgreSQL subclasses select Psycopg and a dedicated schema, using the same repository operations and scope codecs. The port accepts only fixed internal SQL with qmark parameters, translated to Psycopg placeholders; values remain bound separately. It is not a general SQL parser or untrusted query API.

Use two packaged, ordered SQL migrations: the initial tables, followed by database scope guards and indexes. Record SHA-256 checksums and reject changed, unknown or gapped history. Migration runs are explicit and atomic, including DDL and ledger entries. Adapters require the current schema and never perform automatic upgrades. No unscoped SQLite data is imported or assigned guessed identities.

Serialize writes per schema with a PostgreSQL transaction advisory lock. This covers absent-row races, duplicate evidence IDs, global knowledge versions, preview generation comparison and checkpoint idempotency. Writers use READ COMMITTED and acquire the lock before reading domain state. Read snapshots use REPEATABLE READ, preserving version/fact consistency. Audit events commit with their state updates; they are not a delivery outbox.

## Consequences

This intentionally limits write throughput and still scans verified findings in memory. It is adequate for an initial single-service pilot, not a scale claim. A future finer-grained lock/index design must preserve global knowledge-version and absent-row semantics and supply regression evidence before replacement. Separate adapter instances/connections are required per thread.

The lock is cooperative, not access control. Database writers can bypass application rules. Stop application writers for upgrades, restrict DDL to migration operators, and use reviewed private configuration for credentials, TLS, least-privilege grants, backups and retention. This increment does not supply those operational controls, a connection pool, an authenticated evidence API or an atomic external deployment fence.

Transaction behavior follows [Psycopg transaction contexts](https://www.psycopg.org/psycopg3/docs/basic/transactions.html), [PostgreSQL transaction advisory locks](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS) and [repeatable-read isolation](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-REPEATABLE-READ). Integration tests verify the chosen behavior rather than inferring correctness from the API alone.
