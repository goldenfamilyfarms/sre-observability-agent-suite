# ADR-003: shared evidence-backed semantic memory and graph

Status: governance boundary accepted; PostgreSQL proposed. Date: 2026-11-07.

Decision: all workers read a shared, versioned knowledge layer and submit proposed findings with provenance. Canonical writes pass through reconciliation. Store semantic, episodic, procedural, working and evidence records, plus typed relationships. Scope knowledge by service, environment, source revision and observation time. Maintain transactional events for change invalidation.

Prefer PostgreSQL for canonical records and relationships. Evaluate pgvector on a bounded retrieval benchmark; defer a specialized graph database until relational traversal is insufficient. SQLite is only the local contract-test adapter.

Alternatives: per-agent chat memory permits contradictory truths and loses explicit provenance. A vector store alone cannot provide canonical relation/version semantics. A separate graph engine adds operational complexity before query requirements are measured.

Consequences: context snapshots have versions, stale evidence is filtered, optimistic writes reject stale readers, and conflicting facts are disputed. Production requires authenticated stewardship, source digest verification, corroboration/supersession policies, access control, migrations, backups, outbox delivery and retrieval quality evaluation. Those capabilities are not implied by the local adapter's `verified` state.
