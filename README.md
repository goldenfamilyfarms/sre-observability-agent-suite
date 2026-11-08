# SRE Observability Agent Suite

A public, generic foundation for Jira-driven observability engineering using Claude Code workers, an external durable orchestrator, and shared evidence-backed knowledge.

**Status: Phase 0 assessment and a local foundation.** This is not a deployed agent platform. No live Jira, Claude Code, Temporal, Grafana, cloud, merge, or deployment adapter is included. All examples are synthetic.

## Run locally

Python 3.11 or newer; runtime and tests use the standard library only.

```sh
python -m unittest discover -v
python -m sre_suite.demo
```

The demo runs a fake engineering worker against scoped knowledge, records a ticket checkpoint, closes and reopens storage, and confirms recovery. It performs no external calls. A successful demo does not prove distributed orchestration or real engineering changes.

For the same static checks as CI:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m mypy sre_suite
python -m ruff check .
python -m ruff format --check .
```

## Included

- Immutable typed execution tickets, scopes, evidence, worker requests/results, releases, and approval records.
- Dependency validation with cycle, duplicate, and missing-prerequisite rejection.
- SQLite reference memory with evidence provenance, optimistic versions, conflict detection, graph edges, snapshots, and transactional change events.
- Ticket transition guards and restartable local checkpoints with idempotency-key conflict detection.
- Default-deny promotion policy: exact-release non-production authorization and explicit human approval for production.
- A versioned **120-slot design catalog** (40 traces, 40 logs, 40 PromQL) and report aggregation. Query/assertion implementations are intentionally absent; this draft cannot issue a passing certificate.
- Unit/adapter tests, a synthetic vertical slice, and CI configuration with no shipping steps.

## Design and delivery

- [Grounded Phase 0 assessment](docs/phase-0-assessment.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Architecture decisions](docs/adr/README.md)
- [Phased plan and acceptance gates](docs/implementation-plan.md)
- [Dynamic GCP preview environments: GitHub, Helm and Argo CD](docs/dynamic-environments.md)
- [Agent contracts](docs/agent-contracts.md)
- [Certification design](docs/certification.md)
- [Validation and limitations](docs/validation.md)
- [Public repository and security boundaries](SECURITY.md)

The public repository contains only generic design and synthetic fixtures. Runtime evidence, credentials, internal service inventories, and infrastructure configuration belong in separately authorized private systems.
