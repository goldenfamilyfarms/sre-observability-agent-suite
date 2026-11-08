# SRE Observability Agent Suite

A public, generic foundation for Jira-driven observability engineering using Claude Code workers, an external durable orchestrator, and shared evidence-backed knowledge.

**Status: Phase 1 started; preview identity, PostgreSQL persistence, and authenticated evidence/context API implemented.** This is not a deployed agent platform. No live Jira, Claude Code, Temporal, Grafana, cloud, merge, or deployment adapter is included. All examples are synthetic.

## Run locally

Python 3.11 or newer. The SQLite core and default tests use only the standard library. PostgreSQL is an optional extra.

```sh
python -m unittest discover -v
python -m sre_suite.demo
```

The demo registers a preview generation, verifies local evidence bytes, runs a fake engineering worker against shared scoped knowledge, and reopens persisted knowledge/checkpoints. It performs no external calls. A successful demo does not prove distributed orchestration or real engineering changes. Package 0.4.0 preserves the 0.2.0 requirement for explicit environment IDs/generations and a deliberate [legacy storage upgrade](docs/phase-1-foundation.md#upgrading-local-010-storage).

For the same static checks as CI:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt ".[postgres]"
python -m mypy sre_suite
python -m ruff check .
python -m ruff format --check .
```

## Included

- Immutable typed execution tickets, scopes, evidence, worker requests/results, releases, and approval records bound to explicit environment instance and generation.
- Persistent preview generation registry with optimistic updates, expiry checks, idempotent retries and closure tombstones.
- Bounded local evidence storage with SHA-256 content verification, shared ticket context and stale worker-result rejection.
- Dependency validation with cycle, duplicate, and missing-prerequisite rejection.
- Authenticated HTTPS/WSGI evidence upload/read and scoped context API with exact-scope service grants; see the [API guide](docs/authenticated-api.md).
- Optional PostgreSQL adapters with transactional, checksummed migrations and real database integration tests; see the [PostgreSQL guide](docs/postgresql.md).
- SQLite reference memory with evidence provenance, optimistic versions, conflict detection, graph edges, snapshots, and transactional change events.
- Ticket transition guards and restartable local checkpoints with idempotency-key conflict detection.
- Default-deny promotion policy: exact-release non-production authorization and explicit human approval for production.
- A versioned **120-slot design catalog** (40 traces, 40 logs, 40 PromQL) and report aggregation. Query/assertion implementations are intentionally absent; this draft cannot issue a passing certificate.
- Unit/adapter tests, a synthetic vertical slice, and CI configuration with no shipping steps.

## Design and delivery

- [Grounded Phase 0 assessment](docs/phase-0-assessment.md)
- [Phase 1 implementation, breaking changes and storage migration](docs/phase-1-foundation.md)
- [Authenticated evidence and scoped context API](docs/authenticated-api.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Architecture decisions](docs/adr/README.md)
- [Phased plan and acceptance gates](docs/implementation-plan.md)
- [Dynamic GCP preview environments: GitHub, Helm and Argo CD](docs/dynamic-environments.md)
- [Agent contracts](docs/agent-contracts.md)
- [Certification design](docs/certification.md)
- [Validation and limitations](docs/validation.md)
- [Public repository and security boundaries](SECURITY.md)

The public repository contains only generic design and synthetic fixtures. Runtime evidence, credentials, internal service inventories, and infrastructure configuration belong in separately authorized private systems.
