# ADR-010: Temporal execution pilot with restricted Claude Code and Jira adapters

Status: accepted for the Phase 1 pilot; production hosting and operations remain undecided.

## Decision

Use the optional pinned Temporal Python SDK for durable engineering workflows. All HTTP, filesystem and process work occurs in activities. Workflow history contains scope and artifact references; private source/context/output remains in the evidence service and a durable filesystem journal. Activities revalidate the exact preview ID/generation, ticket, execution policy, evidence and context before advancing.

Implement Jira Cloud ticket intake from a versioned JSON text field, explicit dependency checks, and opt-in idempotent execution-property writeback. Implement real Claude Code invocation with restricted/bare mode and no model tools: bounded structured edits pass through host validation, separate review, network-disabled container tests and separate QA. Return a patch for human review. No merge, deployment or production execution is implemented.

## Evidence and consequences

Tests use a real temporary Temporal service for history replay, bounded retry, cancellation, duplicate workflow rejection, and worker restart after a saved stage loses its acknowledgement. An executable CLI fixture exercises the subprocess protocol; Docker executes the proposed code. A simulated lost Jira PUT response is reconciled without a duplicate write. Real Jira credentials and paid model completions have not been exercised.

The private shared journal provides retry reuse with checksums, atomic writes and file locks. It requires durable storage and tested shared locking. A crash before saving model output can repeat a billed call; exact-once model billing is not claimed. Separate model sessions currently share a service principal. Production approval signals, workflow upgrade compatibility, server disaster recovery, cross-host journal recovery, live-account acceptance and authenticated reviewer separation remain open gates from ADR-002.

Alternative: retaining only the local checkpoint prototype would not demonstrate external durable execution. A full autonomous shell worker would enlarge the permission boundary before live acceptance. This pilot instead delivers reviewable edits with an explicit file allowlist and configured test command. See the [runtime guide](../execution-runtime.md) for setup, limits and operational responsibilities.
