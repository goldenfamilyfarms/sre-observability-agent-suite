# Candidate-bound engineering telemetry

Version 0.15 adds an optional telemetry step to `TicketExecution`. An operator-reviewed private configuration authorizes bounded collection and evidence uploads when the operator explicitly starts the engineering workflow. Tickets and model output cannot enable it. The default engineering-only path remains available; its `ready_for_review` result makes no telemetry claim.

## What the evidence identifies

Every bound query carries the five existing scope dimensions plus `binding.artifact_digest` and `binding.plan_digest`. The collector renders all seven selectors and checks returned Prometheus/Loki labels or matching Tempo resource attributes. Wrong or missing candidate labels fail; matching unrelated scope alone is insufficient. Cross-service trace context remains explicitly attributed.

The artifact is a **source candidate**, identified as `source-candidate-v1`. Its digest is `sha256:` followed by SHA-256 of the UTF-8 canonical JSON recipe below (sorted JSON keys, compact separators, `ensure_ascii=True`; no newline):

```json
{
  "kind": "source-candidate-v1",
  "repository": "example/demo-service",
  "base_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "edits": [{"path": "app.py", "content": "complete replacement file contents\n"}]
}
```

Sort edits by path. The immutable base commit identifies the whole original tree; edits identify complete replacement contents. Existing file modes are preserved and new files use `100644`, matching the current publication contract. This is distinct from the saved implementation-result hash, Git commit SHA and OCI image digest. The latter needs a future independently verified build/provenance contract. The plan digest is `sha256:` plus the frozen execution manifest checksum: ticket and acceptance criteria, context, selected source, execution spec and trusted runtime policy including telemetry configuration. It is not the original planning backlog DAG hash. Fixed observation windows and policy fingerprints are bound separately in the saved telemetry plan/evidence.

`scope.revision` continues to mean the immutable **base** revision from the ticket. Map it to an appropriate private source attribute; use the artifact binding to identify the candidate. A build adapter must attach the exported digests only to the actual corresponding build, never blanket-label unrelated running services. These checks verify backend-reported attribution; they cannot authenticate instrumentation truth or prove which image was deployed.

## Private configuration and workers

1. Copy [the synthetic example](../examples/engineering-telemetry.json) to an owner-only file (`0600`) in a physical private directory (`0700`) outside Git. Replace scope, endpoints, credential-variable names, query templates, mappings and `producer`. That producer must equal the telemetry writer's authenticated evidence API subject; its credential needs exact-scope evidence read/write and context grants. Do not insert credential values in this file.
2. Configure one to three profiles, at most one per backend. Each needs two distinct `binding_labels`, separate from all five scope mappings. Tempo requires `allow_full_traces: true` and a nonzero 32-character hex `trace_id` for a planned synthetic transaction. This release does not discover a new trace ID dynamically. Supply the planned trace context externally, or start a new reviewed run with a known trace. Search summaries cannot satisfy this integration.
3. Save the exact nested `policy` object in a separate private telemetry-policy file. Run the existing [telemetry worker](telemetry-scheduling.md) with that policy and its own durable private journal, in the same Temporal namespace. Set its `SRE_TELEMETRY_TASK_QUEUE` to the configured `task_queue`. Keep backend credentials exclusively in that worker's environment. Both workers must reach the same authenticated evidence API. The runtime verifies the exact policy fingerprint before any source query.
4. Set `SRE_ENGINEERING_TELEMETRY_CONFIG` to the absolute private integration-file path in the engineering worker environment. Local-pilot `worker.json` accepts this path, but the local-pilot lifecycle does not start the telemetry worker. The engineering worker receives no telemetry source credentials. `check-config` and preflight validate the file; preflight does not prove telemetry worker availability, source access or deployment provenance.
5. Review `delay_seconds` (0–1800) and `window_seconds` (1–300). Collection starts after all four engineering stages and optional draft publication. Its fixed window starts on the next whole second plus the delay, then lasts the configured duration. The saved stage evidence must outlive the window and the 120-second lateness budget. Start the workflow explicitly only after arranging any separately authorized preview automation.

```sh
python -m sre_suite.execution.cli check-config
python -m sre_suite.execution.cli preflight /absolute/private/execution.json
python -m sre_suite.execution.cli start /absolute/private/execution.json
```

Do not also submit the generated child schedules with the standalone scheduler start command: the parent owns those workflow IDs. Each profile runs one durable child collection on the dedicated queue, concurrently with the other profiles. Parent history carries only schedule/binding metadata and evidence references; telemetry bodies remain private.

## Candidate handoff and readiness

With draft publication enabled, the reviewed/tested candidate is published before collection. Existing GitHub enable flags still apply. The parent exposes its frozen plan through the `telemetry_plan` query while waiting. Export it to a **new** file in a private directory:

```sh
python -m sre_suite.execution.cli export-telemetry-plan WORKFLOW_ID \
  --output /absolute/private/candidate-telemetry-plan.json
```

The plan includes the two digests, artifact kind, implementation reference, fixed schedules and optional draft PR/head. Separately authorized preview tooling can consume this file and the candidate. This repository does not build/deploy that preview automatically. If external setup misses the window, collection blocks; it does not shift the window or relabel stale telemetry. Use a new reviewed run ID after resolving the cause.

Before recording readiness or transitioning Jira to review, the engineering activity reads the actual evidence bytes through the authenticated API, verifies content digest and producer, exact scope/generation, candidate/manifest binding, query profile, fixed window, context version, timestamps and expiry, then rechecks returned candidate attribution. Every configured source must have one complete nonempty `observed` result without issues. Missing/tampered references fail closed. Partial, empty or failed collections yield a blocked report. Scope/context and candidate PR head are rechecked around publication. Drafts created before a later failure remain open for review; the workflow never merges or deploys them.

`ready_for_review` means the engineering gates and configured observation requirements passed. A metric value such as `up=0` is still an observation, not a health assertion. `certification_verdict` remains null. Version 0.16 adds the [separate explicit 120-check certification engine](certification.md). Operators supply independently executed fixtures; deployment provenance remains separate.

## Recovery and cancellation

Prepared schedules and candidate identity are journaled once; activity retries and parent-worker restart preserve them. Telemetry captures are saved before idempotent upload and reused after lost acknowledgements without refreshing timestamps. The evidence API namespaces IDs by producer and scope; receipts carry those server IDs. Evidence expires after 15 minutes and cannot be refreshed by replay. All five scope dimensions and current context remain fenced even on retry.

Canceling a parent cancels its children, including durable waits; active collection uses the existing heartbeat/process cancellation. Parent close requests child cancellation and child execution is bounded to one hour even if workers are unavailable. Cancellation cannot undo evidence or draft PR writes already committed. A missing worker, expired preview, stale knowledge or closed/changed candidate blocks progress. Replacement workers need the same private configuration and durable journal; cross-host storage disaster recovery and live backend acceptance remain unverified.

Old recorded prepare results have no telemetry marker and replay the original engineering path. Do not change a worker's policy halfway through an execution: manifest checks reject it. Complete/drain existing runs before enabling a changed configuration, or restart with a fresh reviewed run ID.

Tests cover protocol attribution for all three backends, actual authenticated evidence, changed candidates/plans, missing/forged evidence, expiry/generation fences, draft-publication recovery, real Temporal child workflows, lost acknowledgements, replay, restart and cancellation. All fixtures and credentials are synthetic.

Version 0.17 adds an optional required [certification gate](engineering-certification.md) after these observations. It verifies all 120 slots through the separate verifier workflow and rechecks the current certificate before final review readiness. Without that configuration, telemetry readiness alone remains an observation result.
