# Architecture decision records

| ADR | Decision | Status |
| --- | --- | --- |
| [001](001-modular-python-foundation.md) | Small typed Python core and local adapters | Accepted for foundation |
| [002](002-external-orchestration.md) | External orchestration; evaluate Temporal | Boundary accepted; product selection proposed |
| [003](003-shared-evidence-memory.md) | Shared versioned facts/graph with evidence | Boundary accepted; PostgreSQL candidate |
| [004](004-shipping-authorization.md) | Exact-release authorization and human production approval | Accepted |
| [005](005-certification.md) | Versioned 40/40/40 certification with fail-closed reports | Accepted; query implementation pending |
| [006](006-dynamic-preview-environments.md) | Authorized GCP previews through Helm and Argo CD GitOps | Proposed; pilot and instance-scoped contracts required |
