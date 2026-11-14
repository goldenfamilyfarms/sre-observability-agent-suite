# ADR 027: Pinned source discovery and independent assessment before planning

Status: Accepted for bounded non-production discovery.

## Context

Planning previously required a supplied dependency DAG. It could validate evidence and publish tickets but could not generate an architecture manifest or explain instrumentation gaps. Source inspection must not execute repository instructions, confuse static markers with runtime proof, or weaken the candidate/plan/preview generation gates.

## Decision

Add deterministic Intake/Architecture and Assessment agents. Intake reads an explicitly selected, size-bounded set of Git blobs at an exact commit, bound to an operator-pinned inventory SHA-256. It publishes hash/line-attributed static indicators and an interaction-category graph, without source snippets or endpoint strings. Assessment runs with a different API subject and queue, consumes the authenticated manifest, and publishes an independently derived five-control gap register and unverified static-coverage score.

An explicit Temporal workflow coordinates intake, assessment and draft planning using credential-filtered child processes, immutable retry journals, deadlines, cancellation and separate queues. It produces a dependency-ordered plan without Jira writes or engineering dispatch. Planning provenance binds both evidence producers, the request, source artifact, scope/context and rule-policy digest. Existing planning dry-run, digest approval, publication and handoff recompute the assessment and exact proposed plan. Legacy manual plans remain compatible; changed provenance or target identities invalidate approval digests.

## Consequences

This provides useful, testable baseline discovery without model or infrastructure credentials. Static indicators are fallible and selected-file coverage is explicit. No source absence becomes a capability exclusion, no score establishes runtime maturity, and no manifest passes certification. Live topology, AST-based discovery, multi-repository plans, authenticated human planning approval and automatic dependency execution remain separate work. The source snapshot is not a future engineering candidate or deployed image. Source changes require fresh evidence and scope.

The operator must provision distinct scoped identities and private request/journals, review coverage and configure worker placement or reliable shared storage. Existing local pilot startup does not silently add these privileges. Temporal history controls durable execution; journals preserve immutable external-publication inputs only. No merge, deployment, production approval or privileged memory reconciliation capability is added.

See [operator guide and validation](../intake-assessment.md).
