# Agent contracts

These contracts define the intended worker boundary. This PR implements common `AgentRequest`, `AgentResult`, `EngineeringWorker`, and `JiraPort` types, a fake worker and result binding checks. The specialized roles below are designs, not installed agents.

Every request binds execution ID, validated Jira ticket, exact scope (including environment ID/generation), context version, attempt limit (1–3) and timeout (1–3600 seconds). Every result binds the same identifiers and includes scoped evidence. The local PreviewCoordinator now validates current preview generation, shared context and stored evidence bytes at preparation/completion. An authenticated dispatcher, enforced execution budgets and external side-effect fencing are still required. An outcome string does not by itself pass a workflow gate.

| Role / purpose | Required input → output | Allowed tools and side effects | Memory permission | Consumes → publishes | Success / failure |
| --- | --- | --- | --- | --- | --- |
| Planning: decompose assessment | Assessed service context → dependency DAG, execution tickets, dry-run | Authorized repository/knowledge reads; Jira creation only after approved dry-run | Read scoped facts; propose plans/decisions | AssessmentReady → PlanProposed / PlanValidated | Complete acyclic plan; ambiguous requirements block |
| Implementation: engineering change | Ticket + context + baseline → branch diff, tests, evidence | Isolated repository edit and bounded test commands; branch creation in assigned workspace | Read scoped facts/patterns; propose findings | TicketReady → ImplementationCompleted / Failed | Validated diff and tests; failures are reported, never self-approved |
| Code review: independent assessment | Exact diff and acceptance criteria → actionable findings/verdict | Repository reads and bounded analysis; no implementation edits or merge | Read context; append review evidence | ImplementationCompleted → ReviewPassed / ChangesRequested | Independent evidence-based verdict; unresolved material finding blocks |
| QA: independent behavior checks | Acceptance criteria and candidate → test plan, results and verdict | Isolated fixture/test execution; no shipping | Read relevant context; propose QA evidence | PlanValidated or ImplementationCompleted → QAPassed / QAFailed | Required positive/negative tests pass; untestable criteria block |
| Grafana query: retrieve telemetry | Authorized source + query + window → raw result reference/digest or typed error | Read-only, bounded TraceQL/LogQL/PromQL; no datasource/config writes | Read query context; propose evidence | QueryRequested → QueryCompleted / QueryError | Response captured and attributed; query success never implies requirement PASS |
| Verification: evaluate requirements | Approved checks, release, fixtures/window → deterministic results/report | Authorized synthetic fixtures and query port; no code or production mutation | Read context; propose verification evidence | DeploymentHealthy / VerificationRequested → Verified / Failed / Incomplete | Independent assertions with exact attribution; uncertainty cannot pass |
| Shipping: controlled delivery | Reviewed change + CI/QA + release policy → PR/status/deployment references | Only authorized GitHub/Jira/CI/deployment operations; production requires human approval | Read scoped state; append delivery evidence | QAPassed → PROpened / ApprovalRequested / EnvironmentAccepted | External state reconciled and verified; failure blocks or requests rollback |

All roles have bounded attempts, deadlines and cancellation. Retry transient read failures with bounded backoff. Do not retry writes until their idempotency key and remote state are checked. Schema, authorization and policy failures are non-retryable until corrected. Exhaustion produces a blocked execution, not an autonomous infinite remediation loop.

Human approval is mandatory for production promotion. Non-production automatic promotion requires explicit release authorization. Knowledge reconciliation is steward-controlled. Tool permissions are enforced outside prompts; the worker cannot widen them. The implementation actor cannot act as reviewer, QA approver or certifier for the same execution.

## Additional typed boundaries to introduce

- `KnowledgePort`: service/ticket context, dependency graph, search, snapshot, proposal, decision, execution event, reconciliation, agent-event publication.
- `TelemetryPort`: authorized query plan, datasource ID, bounded window, timeout, structured result reference and error taxonomy.
- `ApprovalPort`: authenticated approver identity, verified authorization policy, exact-release approval, expiry, revocation and audit event.
- `ShippingPort`: idempotent intent/result reconciliation for PR, merge and deployment operations.

The local memory and checkpoint classes are reference adapters with trusted callers. They must not be exposed directly to workers as privileged remote APIs.
