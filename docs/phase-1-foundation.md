# Phase 1, increment 1: scoped preview execution foundation

Status: implemented and tested locally with SQLite and a synthetic worker. This increment closes the initial ENV-1 contract gap and starts the evidence/context portion of Phase 1. It does not complete Phase 1 or provide a live preview controller.

## Identity contracts

Package version 0.2.0 intentionally breaks the unscoped 0.1.0 constructor contract. Every `Scope` now requires:

```python
from sre_suite.contracts import Environment, Scope
from sre_suite.previews import preview_environment_id

scope = Scope(
    service="demo-service",
    environment=Environment.DEVELOPMENT,
    revision="a" * 40,  # Synthetic full commit SHA.
    environment_id=preview_environment_id(repository_id=1, pr_number=142),
    generation=1,
)
```

The environment class describes policy, while `environment_id` identifies an instance. `generation` identifies a specific incarnation of that instance. Even a rebuild of the same source revision must use a new generation when it replaces the preview request. Stable environments also need an explicit ID and positive generation; there is no implicit default or wildcard. The preview ID helper uses GitHub numeric repository ID and PR number, avoiding repository slug/branch-name normalization collisions. Other SCM providers need a separately namespaced identity scheme.

IDs must be lowercase DNS labels of at most 63 characters. Generations and numeric repository/PR identifiers are positive signed 64-bit integers; booleans, floats and numeric strings are rejected. `encode_scope`/`decode_scope` define a version-2 JSON envelope, reject unknown/missing fields, unsupported versions and duplicate JSON keys. This is a scope codec, not a complete untrusted request parser for every domain class.

Because evidence, tickets, findings, worker inputs/results, releases, approvals and certification results embed Scope, equality and existing policy checks bind all of them to the same instance and generation. Certification report schema advances to version 2. The draft 120-slot catalog is unchanged and still cannot certify live services.

## Persisted generation control

`PreviewRequest` binds repository ID, PR number, scope and a finite observation-independent validity window. Production is rejected. `PreviewRegistry.start(request, expected_generation, now)` atomically compares the previous generation and persists the request with an audit event. The first generation is 1; every subsequent generation must be exactly the prior one plus 1. A retry of the identical active request is idempotent. Changed input at the same generation, a late older request, and competing updates are rejected.

`require_current(scope, now)` rejects missing, closed, expired or superseded previews. `terminate(scope, reason, now)` persists a tombstone and an event without claiming that cloud resources have been deleted. A delayed request cannot recreate the closed generation; a trusted controller must explicitly request the next generation to reopen it. Stale close events cannot terminate a newer generation. State and events commit together; the tests terminate a subprocess during that transaction and verify rollback after reopening storage.

This is a single-service pilot registry. It supplies concurrency primitives, not an authenticated PR event handler, authorization service, TTL sweeper or resource lifecycle executor. The trusted future controller must enforce current PR head, closure, permissions, quotas, maximum TTL and generation allocation. There is no automatic provisioning or deployment.

## Local evidence and shared context

`EvidenceStore` persists at most 1 MiB per evidence object and verifies the supplied SHA-256 digest against the bytes. IDs are immutable: replay is allowed only with identical metadata and content. Verification rechecks metadata, digest, scope and time validity. Knowledge reconciliation now requires an `EvidenceVerifier`; unknown, modified, stale or differently scoped evidence cannot become a canonical finding. Reads exclude facts whose evidence no longer verifies. Persisted evidence scopes are decoded independently of their findings so a mismatch is rejected rather than overwritten during deserialization.

This checks local content integrity, not whether a producer is authentic or its claim is true. The store does not fetch arbitrary URIs, contact telemetry APIs, authenticate a human, encrypt a database, or enforce tenant permissions. A future private evidence service must add those controls.

`KnowledgeStore.get_ticket_context` checks ticket evidence and returns a verified instance-scoped snapshot. `PreviewCoordinator.prepare` first checks current generation/expiry and caps the returned context at 200 findings. `validate_completion` rechecks preview state, the entire current snapshot, request/result identity and every result/finding evidence object. Any knowledge change, expiry or content mismatch requires rebuilding context. The knowledge version is global to the local store, so an unrelated reconciliation conservatively invalidates outstanding snapshots too.

The coordinator's promotion check combines the existing exact-release authorization policy with current preview validity and evidence-content verification. No boolean verdict grants real shipping rights. The functions are local point-in-time checks, not an atomic transaction with an external deployment. A future executor must use a generation fence and revalidate immediately before a side effect; distributed outbox/reconciliation remains required. Production approval semantics are unchanged: explicit authorized human approval bound to the full release, now including instance and generation.

`Checkpoints(path, scope)` binds each run ID to a complete v2 scope. Reads, writes and idempotent replays under a different instance or generation fail. Run IDs should be unique per execution; a new preview generation starts a new execution rather than adopting old checkpoints.

## Upgrading local 0.1.0 storage

Old facts and checkpoints have no trustworthy environment-instance attribution. The code refuses to load their tables automatically. It never assigns a guessed ID/generation to historical evidence or approvals.

1. Stop all writers and retain a consistent SQLite backup, including committed journal/WAL state. Use SQLite's backup facility rather than copying a live database file alone.
2. Inspect the intended local database and call `sre_suite.storage.quarantine_legacy(path)` explicitly.
3. The operation atomically renames old tables to `legacy_v1_metadata`, `legacy_v1_findings`, `legacy_v1_events`, `legacy_v1_checkpoints` and `legacy_v1_operations` where those complete legacy components exist. Rows remain unchanged for audit. A partial legacy schema or conflicting archive aborts the entire transaction. Unknown schema versions are rejected.
4. Open the v2 adapters. New scoped tables are initialized separately; archived records are not included in context or resumed execution. Repeating quarantine is a no-op once complete.
5. Collect fresh evidence with the correct identity and restart scoped runs. If an operator wishes to recover historical facts, a separate reviewed attribution/reconciliation process is required; this increment supplies no automatic promotion of archived records.

Existing v2 databases are left intact. Backups and archived tables may contain private data and must stay outside the public repository. Rollback requires stopping writers and restoring the pre-upgrade backup with version 0.1.0; do not point the old binary at a v2 database or overwrite v2 work without preserving it.

## Validation and remaining Phase 1 work

The suite now has 70 tests, including the latest Phase 0 graph regression and 30 added tests covering strict identity, wire compatibility, evidence integrity, consistent worker snapshots, generation races/retries/tombstones/expiry, scope-bound checkpoints, cross-instance approvals/certification, migration preservation/rollback and interrupted transaction recovery. `python -m sre_suite.demo` exercises the new local evidence, preview, context and checkpoint path without external calls. The Phase 0 single-valued-predicate configuration is preserved: supply it as the third KnowledgeStore argument alongside the required evidence verifier; multi-target graph edges remain valid.

The subsequent [PostgreSQL increment](postgresql.md) implements the adapter and migrations. The [authenticated HTTP API increment](authenticated-api.md) adds pilot evidence/context endpoints. Still required: an outbox/dispatcher, distributed identity/audit and MCP, governed reconciliation/supersession, Temporal replay/retry/cancellation evaluation, and a real isolated Claude Code worker with enforced tool/time/cost controls. ENV-2 onward also needs authorized GCP/Argo/Helm integrations, protected desired-state publication and verified resource cleanup. None of those capabilities is represented as complete by this increment.

## Increment 4: durable execution pilot

Version 0.5.0 adds optional Temporal orchestration, restricted Claude Code CLI execution, scoped Jira ticket intake/writeback, isolated container tests and artifact recovery. Existing version 2 scope contracts and database migrations remain unchanged. Follow the [runtime guide](execution-runtime.md); live-account acceptance remains pending private configuration.

## Increment 5: review artifact publication

Version 0.6.0 adds explicit GitHub PR publication and configured nonterminal Jira transitions without changing preview contracts or database migrations. Drain/reconcile old runs before upgrading: the manifest policy fingerprint now includes publication authority. See [setup, retry and partial-completion behavior](pr-and-jira-publication.md).

## Increment 6: read-only preflight

Version 0.7.0 adds a specification-bound readiness report and read-only service/local prerequisite probes. Preview contracts, database migrations, runtime policy fingerprints and execution authorization remain unchanged. See [preflight exit codes and limitations](preflight.md); a successful read observation does not prove write or inference authority.

Version 0.8.0 adds an opt-in controller registration endpoint and [pilot setup command](pilot-setup.md), with separate exact-scope registration authority, idempotent evidence upload and private execution-spec output. Worker grants, runtime generation fencing, PostgreSQL migrations and production policy remain unchanged.
