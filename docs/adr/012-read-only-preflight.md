# ADR-012: read-only preflight without implied write authorization

Status: accepted for the execution pilot.

Decision: add a specification-bound `preflight` CLI command that inspects local prerequisites and remote read APIs, then reports verified failures, observations and unverified capabilities separately. Preserve the full preview ID/generation and recheck the scope/context/evidence at the end. Keep execution and shipping authorization in their existing runtime gates.

A read request cannot prove successful publication, a Jira transition, an evidence upload, paid model inference or durable journal writes. Report those gaps explicitly instead of performing temporary mutations or treating account-level permissions as token write authority. The pilot returns `needs_validation` (exit 2) when its read checks pass but live checks remain; failures return `blocked` (exit 1).

The HTTP adapter enforces GET plus the two existing read-semantic evidence/context POST routes. Temporal uses describe RPCs only. CLI help and Docker image inspection do not invoke models or run containers. Private response bodies, credentials and local paths never enter the JSON report. Temporary source inspection files are cleaned; durable state is not changed.

Consequences: preflight can identify missing credentials, permissions, workers, source revisions and stale previews early, but it cannot reserve state or guarantee an execution will succeed. A recent poller is not proof of current capacity. No preflight artifact grants start/merge/deploy permission. A separate authorized synthetic live pilot remains required. Existing workflows, database migrations and publication policy fingerprints are unchanged. See [usage and limits](../preflight.md).
