# Suite implementation and acceptance ledger

The target stack is Claude Code, Temporal, PostgreSQL, Prometheus, Loki, Tempo, GCP/GKE, Helm and Argo CD. All twelve agent roles in the proposal now have callable bounded v1 implementations. This is not a claim that every roadmap milestone, every framework, or private production operations is complete. The remaining work below is deliberately separated from implemented agent code.

| Proposal role | Implemented code | Validation / private acceptance boundary |
| --- | --- | --- |
| Intake / Architecture | Pinned Git discovery, hash/line indicators, Python AST citations and observed cross-service trace edges | Other framework ASTs are not implemented; static matches are not runtime claims |
| Assessment | Independent static gaps, authenticated standards/topology/health input binding, bounded maturity score and gap register | Private service/control coverage and thresholds must be reviewed |
| Planning | Evidence-derived phased DAG, authenticated publication, service epic, optional initiative parent, child epic links, retry-safe ticket creation, preflight handoff and durable dispatch | Deterministic decomposition; model-assisted planning and automatic cross-repository workstream scheduling remain future code |
| Implementation | Real restricted Claude Code adapter, pinned file edits, immutable stage journal, distinct implementation subject | Live model/CLI compatibility, repository permissions and cost acceptance |
| Code Review | Separate Claude invocation/queue/subject and artifact-chain attestation | Independent reviewer tokens and private acceptance criteria |
| QA | Isolated container test execution, separate QA model/subject and evidence; generic multi-service HTTP journeys | Private images, service endpoints and fault/capability fixtures |
| Grafana Query | Prometheus/Loki adapters, full Tempo traces, finite durable scheduling, exact artifact/plan/source labels | Private backend schemas, TLS, tenants, volume and query acceptance |
| Verification | Independent fixture runner/assessor, all 120 deterministic checks, explicit source/image artifact kinds, fresh gate reader | Private controlled workload, all service assertions and applicability proof |
| Shipping | Draft PR/Jira publication, whole-tree candidate verification, trusted CI checks, independent review, authorized exact fast-forward | Live GitHub permissions/branch rules and isolated CI build attestations |
| Deployment / Readiness | GKE identity/endpoint lookup, pinned Helm rendering, owned Argo Application and namespace lifecycle, actual pod image/recipe/readiness, rollback/cleanup preconditions | Existing GKE/Argo/registry integration and rollout/rollback acceptance; no cluster/account provisioning |
| Remediation / Regression | Full-catalog comparisons, evidence-linked remediation DAG proposals, distinct approved correction attempts and finite escalation | New Jira/source scope and authorization when the source advances; no gate weakening |
| Knowledge Steward | Authenticated proposal/reconcile/supersession/conflict API, source assertion validation, immutable audit/outbox, independent steward | Private producer/predicate policy and recovery/retention acceptance |

Cross-cutting implementations include server-authenticated human production approval, revocation/expiry, explicit production scope registration, independently authenticated engineering roles, PostgreSQL migration 003, current context and generation checks, and finite Temporal orchestration with cancellation and checkpoints. APIs and agents keep credentials out of workflow payloads and public artifacts.

The shared semantic store uses PostgreSQL. Worker artifacts still require a shared durable POSIX journal with functioning locks/fsync for cross-host operation. Object-store artifact replication and database-backed execution leases are not implemented. Cloud IAM, tenant isolation, secret delivery, an isolated image builder, telemetry routing and service-specific policies are provided by the private environment; they cannot be inferred from synthetic tests.

Version 0.26 implements the [isolated image build/publish/attestation adapter and image-bound target fixtures](image-build-and-target-fixtures.md), including a standalone containerized Python reference. Private infrastructure still provisions the isolated build host, registry, credentials and persistent storage; private service policies still require review. The new reference has measured HTTP traces/logs/metrics and does not claim every catalog capability.

Full-roadmap completion still includes optional model-assisted planning, automated cross-repository workstream dispatch, broader language/framework analysis, and organization-specific backup/restore/upgrade/load verification. Do not describe these as implemented. A Spring Boot reference application is not supplied. Existing real-backend demo acceptance covers 76 applicable checks and 44 independently justified capability exclusions, with all 120 slots accounted for; those results do not transfer to the new target or private services.

See [suite operator guide](agent-suite-operations.md), [ADR 028](adr/028-governed-suite-agents.md) and the focused tests for the new contracts. Credentialed live acceptance is explicitly pending. No merge or deployment is authorized by a completed code review or test run.
