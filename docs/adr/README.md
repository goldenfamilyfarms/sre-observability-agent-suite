# Architecture decision records

| ADR | Decision | Status |
| --- | --- | --- |
| [001](001-modular-python-foundation.md) | Small typed Python core and local adapters | Accepted for foundation |
| [002](002-external-orchestration.md) | External orchestration; evaluate Temporal | Boundary accepted; Temporal pilot in ADR-010 |
| [003](003-shared-evidence-memory.md) | Shared versioned facts/graph with evidence | Boundary accepted; PostgreSQL pilot in ADR-008 |
| [004](004-shipping-authorization.md) | Exact-release authorization and human production approval | Accepted |
| [005](005-certification.md) | Versioned 40/40/40 certification with fail-closed reports | Accepted; query implementation pending |
| [006](006-dynamic-preview-environments.md) | Authorized GCP previews through Helm and Argo CD GitOps | Proposed; pilot and instance-scoped contracts required |
| [007](007-instance-scoped-contracts.md) | Require instance/generation scope and quarantine unscoped legacy data | Accepted; implemented locally in Phase 1 increment 1 |
| [008](008-postgresql-persistence.md) | Shared guards, explicit PostgreSQL migrations and serialized pilot writes | Accepted for Phase 1 persistence pilot |
| [009](009-authenticated-scoped-api.md) | Authenticated service grants and transactional scoped evidence/context API | Accepted for Phase 1 preview pilot |
| [010](010-temporal-execution-pilot.md) | Temporal workflow, restricted Claude Code and scoped Jira execution | Accepted for Phase 1 pilot; live-account acceptance pending |
