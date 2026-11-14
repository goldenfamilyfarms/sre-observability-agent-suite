# Phased implementation plan

These are proposed backlog items, not real Jira issues. Each phase requires review of its evidence before advancing. No phase grants permission to merge or deploy.

Phase 1 increment 1 is now implemented locally: ENV-1 identity/generation binding, persistent generation guards, scope-bound checkpoints, explicit legacy quarantine, local evidence-byte verification and scoped worker context validation. See [implementation and migration details](phase-1-foundation.md). Increment 2 adds [PostgreSQL persistence and explicit migrations](postgresql.md), with concurrent-write and rollback tests against real databases. Increment 3 adds the [authenticated evidence and scoped context HTTP API](authenticated-api.md), including exact-scope service grants and transactional generation checks. P1-1/P1-2 now have persistence and a pilot HTTP authentication boundary. Distributed identity/revocation, access audit, governed reconciliation, an outbox and MCP remain open. Increment 4 implements the Temporal engine (P1-3), restricted real Claude Code adapter (P1-4), and Jira intake/result writeback with environment configuration. Real-engine/CLI-fixture tests cover recovery, cancellation and retry; live Jira/model acceptance and operational hosting remain open. See the [runtime guide](execution-runtime.md). Increment 5 adds [automatic draft PRs and Jira transitions](pr-and-jira-publication.md) with exact-candidate reconciliation and generation guards. Increment 6 adds [read-only preflight](preflight.md) to identify blockers before live acceptance. Phase 1 is not complete.

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
| ENV-1 — instance-scoped contracts and authorization | Implemented and tested locally in Phase 1 increment 1; trusted external authorization still required | Preview ID/generation bound to evidence, memory, releases and approvals; cross-preview reuse rejected; legacy quarantine and checkpoint migration tested |
| ENV-2 — shared foundation and namespace controls | ENV-1; authorized Phase 2/3 pilot setup | Non-prod project/cluster, registry, routing, identity, quotas, enforced network policy and isolated data verified for one synthetic service |
| ENV-3 — gated configuration publisher and Helm ApplicationSet | ENV-2 plus successful CI and exact-release authorization | Correct digest only; stale/failed builds and unauthorized/fork PRs cannot publish; namespaces prepared before workload sync |
| ENV-4 — independent QA and telemetry integration | ENV-3; Phase 3 | Readiness/smoke tests and targeted telemetry evidence identify the exact preview generation; final certification remains 120-slot accounting |
| ENV-5 — deletion, expiry and cost controls | Build alongside ENV-2/3; required before pilot acceptance | Close/unlabel/TTL and missed-event recovery delete all owned resources; partial cleanup retries bounded; no shared-resource deletion |
| ENV-6 — controlled expansion | ENV-1..5; Phase 4 | Complete create/update/delete pilot, quotas/TTL/rollback evidence and reviewed authorization policy before enabling additional services |

### Core execution milestone

Increment 4 supplies the engine and adapters below, with synthetic Jira/model fixtures and real Temporal/container tests. The remaining milestone is an authorized live synthetic ticket, operational recovery/upgrade proof and separately authenticated reviewer identities. Increment 5 adds gated draft PR publication and configured nonterminal Jira transitions; live-account acceptance remains open. Jira planning/issue creation remains Phase 2 work.

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

### Pilot input preparation

The [pilot setup command](pilot-setup.md) registers an explicitly authorized preview generation, uploads and verifies a baseline, and writes a private execution spec. It does not provision ENV-2 infrastructure or satisfy live execution acceptance. The operator must still authorize the exact scope, populate the Jira execution contract with returned evidence, run preflight and separately initiate any live pilot.

The [local startup package](local-pilot.md) makes the bounded Phase 1 pilot repeatable and tests full local stop/start. Real account permissions, paid model execution and a complete live Jira-to-PR acceptance run remain open; local startup does not provision GCP environments or execute the 120-check certification catalog.

Increment 9 adds the [reviewed synthetic demo and live-run checklist](live-pilot-checklist.md), private preparation of baseline/manifest/worker inputs, and an evidence-bound Jira ticket finalizer. Only `app.py` is editable; the unchanged seven-test candidate gate must pass. This prepares the live milestone above without executing it. Credentials, the real Jira issue and live inference/write acceptance remain operator steps.

Version 0.11 adds [bounded Jira planning](jira-planning.md): explicit project/create-field discovery, evidence-backed plan dry-run, stable planned IDs and DAG validation, opt-in issue creation with durable intent/reconciliation, and execution-contract installation with actual Jira dependency keys. This is an initial P2-1/P2-2/P2-3 slice; it does not complete Phase 1 live acceptance or start planned executions. Live account acceptance, model-generated planning, authenticated planner approvals and durable Temporal planning orchestration remain open.

Version 0.12 connects retained Jira publication state to [guarded execution preflight](planning-handoff.md) with actual ticket-key mapping and local configuration reuse. The full target stack and unresolved agent implementations are tracked in [suite completion criteria](suite-completion.md); this does not mark the suite code complete.
