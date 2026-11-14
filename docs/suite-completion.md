# Suite implementation completion criteria

The target stack is Prometheus, Loki, Tempo, GCP/GKE, Helm and Argo CD, confirmed on 2026-11-14. Source and fixtures remain generic; credentials, organization details, source inventories, service schemas and infrastructure values belong in private configuration. A working fixture or an agent name is not sufficient to mark a capability complete.

"Code complete" means a real callable implementation, typed inputs/results, enforced scope/authority/budgets, failure/retry/cancellation behavior, deterministic tests, packaged commands and an operator guide. Credentialed acceptance is a separate status and must never be implied by fixture tests. Existing pilot implementations can be used without completing later production capabilities; they must not be advertised as the whole suite.

| Capability / agent | Current implementation | Remaining code before full suite completion |
| --- | --- | --- |
| Planning | Project discovery, reviewed dependency plan, conservative Jira publication and guarded execution handoff | Model-assisted decomposition, authenticated planner approval, durable planning workflow and events |
| Implementation | Restricted Claude Code edits, Temporal activities, immutable artifact journal and draft PR publication | Broader approved instrumentation patterns and operational worker identity isolation |
| Code review | Separate model invocation against saved candidate; evidence-bound verdict | Separately authenticated reviewer identity and governed change-request loops |
| QA | Network-disabled container gate plus separate QA model invocation | Multi-service fixtures, synthetic journeys and independent QA identity |
| Intake / architecture discovery | Validated supplied tickets/plans and scoped knowledge reads | Evidence-producing repository/service discovery and assessment pipeline |
| Grafana query / telemetry retrieval | [Prometheus/Loki reads](telemetry-adapters.md), [full Tempo traces and finite durable scheduling](telemetry-scheduling.md), [candidate/plan-bound engineering integration](engineering-telemetry.md), private evidence capture, process deadlines/cancellation and generation fences | Live backend acceptance, cross-host recovery, deployed-image provenance and deterministic assertions |
| Verification / certification | Typed report aggregation and 40/40/40 design inventory | Executable assertions/query definitions for all 120 checks, trusted applicability assessment, fixture/window/release attribution and orchestrated execution |
| Synthetic transaction | Design extension point | Allowlisted bounded journeys, scoped propagation and expected-failure fixtures |
| Telemetry pipeline health | Design extension point | Ingestion/exporter/drop/freshness checks and scheduled evidence collection |
| Standards / instrumentation | Generic engineering worker can act on an approved ticket | Versioned approved patterns, automated assessment and evidence-backed recommendations |
| Remediation / regression | Bounded execution retries and failed gates | Governed remediation state machine, regression comparisons, separate budgets and stop conditions |
| Knowledge steward | Trusted local/PostgreSQL reconciliation and authenticated context/evidence reads | Authenticated proposal/reconciliation API, supersession governance, event delivery/outbox and audit |
| Shipping / preview lifecycle | Exact-release policy and guarded GitHub draft PR creation | Authorized GCP/GKE/Helm/Argo CD lifecycle, rollout/reconciliation/cleanup/rollback and retained operation evidence |
| Production approval / operations | Deny-by-default exact-release approval policy | Authenticated human approval service, revocation/audit, backup/upgrade/load/recovery proof |

The live pilot does not have to wait for every row, but the full suite cannot be called code complete until these remaining implementations and their tests exist. Every final certification must account for 120 checks; absence, query success without an assertion, or missing source mappings must never turn into a passing certificate. Production writes continue to require explicit authenticated approval of the exact release.

The handoff release adds no deployment executor and no background autonomous loop. Keep workflow start explicit. Subsequent implementation should use focused reviewable feature branches, maintain these boundaries, and update this ledger with links to code and tests rather than relabeling designs as implementations.
