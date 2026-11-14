# SRE Observability Agent Suite

A public, generic foundation for Jira-driven observability engineering using Claude Code workers, an external durable orchestrator, and shared evidence-backed knowledge.

**Status: Phase 1 execution pilot implemented.** Preview identity, PostgreSQL persistence, authenticated evidence/context APIs, Temporal orchestration, restricted Claude Code workers and Jira adapters are included. Version 0.14 adds full Tempo trace retrieval and explicitly started, finite Temporal telemetry collection with retry-safe evidence capture. The planning-to-pilot handoff generates an execution specification and runs read-only preflight. Workflow start remains explicit. The runtime includes opt-in automatic draft PRs, bounded Jira status transitions and a read-only preflight command. Live Jira/model acceptance remains pending your private environment configuration. No merge, deployment or cloud executor is included; examples are synthetic. Start with the [one-ticket live pilot checklist](docs/live-pilot-checklist.md), [local pilot startup guide](docs/local-pilot.md), [runtime setup guide](docs/execution-runtime.md) and [.env.example](.env.example).

## Run locally

Python 3.11 or newer. The SQLite core and default tests use only the standard library. PostgreSQL and Temporal are optional extras.

```sh
python -m unittest discover -v
python -m sre_suite.demo
```

The demo registers a preview generation, verifies local evidence bytes, runs a fake engineering worker against shared scoped knowledge, and reopens persisted knowledge/checkpoints. It performs no external calls. A successful demo does not prove distributed orchestration or real engineering changes. Package 0.14.0 preserves the 0.2.0 requirement for explicit environment IDs/generations and a deliberate [legacy storage upgrade](docs/phase-1-foundation.md#upgrading-local-010-storage).

For the same static checks as CI:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt ".[postgres,orchestration]"
python -m mypy sre_suite
python -m ruff check .
python -m ruff format --check .
```

## Included

- [Full Tempo traces and durable telemetry scheduling](docs/telemetry-scheduling.md): bounded V2 retrieval, durable timers, recovery, process deadlines and cancellation.

- [Scoped telemetry adapters](docs/telemetry-adapters.md): Prometheus matrix queries, Loki log streams and partial Tempo search summaries; explicit private collection and optional authenticated evidence upload.

- [Planning-to-pilot handoff](docs/planning-handoff.md): verified receipt/journal/Jira mapping, private spec/report output and local configuration reuse.

- [Jira planning](docs/jira-planning.md): project/field discovery, evidence-backed DAG review, conservative issue creation and execution-contract publication.

- Reviewed synthetic demo with immutable acceptance tests, private baseline/manifest generation and evidence-bound Jira ticket finalization.

- Specification-bound read-only preflight with redacted diagnostics, generation checks and explicit unverified write/inference capabilities.

- Optional Temporal engine with bounded retries, cancellation, replay tests and saved-result recovery.
- Real opt-in Claude Code CLI, GitHub draft PR and Jira Cloud adapters, isolated test execution, evidence-backed patches, scoped Jira results and configured nonterminal status transitions.

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

- [Full-suite completion criteria and remaining implementations](docs/suite-completion.md)

- [Demo repository, pilot ticket and live-run checklist](docs/live-pilot-checklist.md)
- [Grounded Phase 0 assessment](docs/phase-0-assessment.md)
- [Phase 1 implementation, breaking changes and storage migration](docs/phase-1-foundation.md)
- [Read-only preflight and readiness report](docs/preflight.md)
- [Automatic draft PRs and Jira status transitions](docs/pr-and-jira-publication.md)
- [Temporal, Claude Code and Jira runtime](docs/execution-runtime.md)
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

Prepare one authorized preview with the [pilot setup command](docs/pilot-setup.md), then run [preflight](docs/preflight.md) against its generated execution spec. Setup registers metadata and uploads one baseline; it does not start work or provision an environment.
