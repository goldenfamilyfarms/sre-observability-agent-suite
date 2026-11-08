# Phased implementation plan

These are proposed backlog items, not real Jira issues. Each phase requires review of its evidence before advancing. No phase grants permission to merge or deploy.

| Phase | Deliverable / proposed work items | Depends on | Acceptance evidence |
| --- | --- | --- | --- |
| 0 — assessed foundation | P0-1 baseline; P0-2 architecture/ADRs; P0-3 typed core and local tests | None | Reviewable PR; recorded test/static results; explicit gap assessment |
| 1 — shared knowledge and execution spike | P1-1 PostgreSQL migrations and evidence service; P1-2 scoped context API/MCP; P1-3 Temporal evaluation; P1-4 isolated Claude Code worker | Phase 0 | Two workers read consistent snapshots; restart/replay tests; conflicts/supersession; crash after side effect; identity/tool/time/cost enforcement; worker produces a real tested change in a synthetic repository |
| 2 — Jira vertical slice | P2-1 authorized project discovery; P2-2 plan dry-run; P2-3 idempotent issue creation; P2-4 implement-ticket workflow; P2-5 independent review/QA | P1-1..4 | `implement DEMO-1` retrieves a validated ticket, checks DAG, records evidence and opens a PR in an approved test repository; repeated delivery creates no duplicate issues/PRs; deliberate QA failure blocks shipping |
| 3 — instrumentation and verification | P3-1 approved OpenTelemetry patterns; P3-2 telemetry read adapters; P3-3 capability mapping; P3-4 query/assertion catalog | Phase 2 | Selected synthetic service demonstrates context propagation, trace/log correlation and metrics; missing telemetry fails; errors/timeouts become ERROR/INCONCLUSIVE; all 120 slots have reviewed applicability, fixtures and deterministic assertions |
| 4 — controlled non-production delivery | P4-1 release workflow; P4-2 CI/GitHub/deployment adapters; P4-3 exact-release authorization; P4-4 rollback | Phase 3 | Explicit non-prod authorization; quality gates enforced; bounded rollout; partial-failure reconciliation; rollback rehearsal; final 120-slot report bound to deployed digest/window |
| 5 — production governance and operations | P5-1 authenticated approvals; P5-2 production gates; P5-3 readiness/backup/load tests; P5-4 regression detection | Phase 4 | Human approval cannot be replayed against a changed artifact; expiry/revocation enforced; independent security/operational review; documented production authorization before any production action |

## Immediate next milestone

### Dynamic-environment workstream

The [GCP preview guide](dynamic-environments.md) and [ADR-006](adr/006-dynamic-preview-environments.md) add a proposed GitHub + Helm + Argo CD + GKE target. These are backlog items, not provisioned environments or real Jira issues:

| Item | Dependency / placement | Acceptance evidence |
| --- | --- | --- |
| ENV-1 — instance-scoped contracts and authorization | Phase 1 contracts, before any preview deployment | Preview ID/generation bound to evidence, memory, releases and approvals; cross-preview reuse rejected; persistence migration tested |
| ENV-2 — shared foundation and namespace controls | ENV-1; authorized Phase 2/3 pilot setup | Non-prod project/cluster, registry, routing, identity, quotas, enforced network policy and isolated data verified for one synthetic service |
| ENV-3 — gated configuration publisher and Helm ApplicationSet | ENV-2 plus successful CI and exact-release authorization | Correct digest only; stale/failed builds and unauthorized/fork PRs cannot publish; namespaces prepared before workload sync |
| ENV-4 — independent QA and telemetry integration | ENV-3; Phase 3 | Readiness/smoke tests and targeted telemetry evidence identify the exact preview generation; final certification remains 120-slot accounting |
| ENV-5 — deletion, expiry and cost controls | Build alongside ENV-2/3; required before pilot acceptance | Close/unlabel/TTL and missed-event recovery delete all owned resources; partial cleanup retries bounded; no shared-resource deletion |
| ENV-6 — controlled expansion | ENV-1..5; Phase 4 | Complete create/update/delete pilot, quotas/TTL/rollback evidence and reviewed authorization policy before enabling additional services |

### Core execution milestone

Prove one real Claude Code worker executing a synthetic engineering ticket under an external durable orchestrator with persistent shared knowledge. Use a non-production demonstration service and separately authorized resources. Do not expand to all supporting agents first.

1. Confirm runtime host, permitted repositories, private evidence destination and budget.
2. Run Temporal acceptance spike; record an accepted/rejected ADR with failure-recovery evidence.
3. Implement authenticated memory/evidence ports and migrations. Prove conflicting concurrent updates and stale-context invalidation.
4. Implement Claude Code isolation, structured output parser, tool allowlist, cancellation and enforced resource budgets. Review repository hooks/configuration before launch.
5. Kill a worker mid-run and recover without duplicating an external write. An uncertain write must reconcile remote state.
6. Add independent review/QA workers. Their authority comes from the dispatcher, not a role string in model output.

## Decomposition and delivery rules

Each executable Jira ticket follows `JiraExecutionTicket`, includes evidence-backed acceptance criteria and a rollback strategy, and identifies dependencies by immutable planned IDs mapped to Jira keys. Shared-library changes precede dependent services. Validate the DAG and show the complete dry-run before creating issues. Stable idempotency keys bind plan version and task identity; changed input cannot reuse a key silently.

QA planning runs alongside implementation. Ticket checks are targeted; final service certification runs all catalog slots. Stop after bounded retries/remediation attempts and preserve evidence for a human. Supporting intake, architecture discovery, synthetic transaction, pipeline health, standards, remediation, regression and knowledge-steward agents are extension points introduced only when the core flow needs them.
