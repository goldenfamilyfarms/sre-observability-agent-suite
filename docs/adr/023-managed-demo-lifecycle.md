# ADR-023: Managed demo lifecycle and export before collection

Status: accepted for the local non-production pilot.

The manual demo controller requires an operator to pin/start the exact target and export its outbox before collection. That timing dependency prevents an unattended explicitly authorized pilot run.

Add an optional immutable `demo` dispatch to the certification plan and fixture identity. A dedicated Temporal activity worker holds a private reviewed policy containing the source recipe, fixture policy, TLS paths and export destinations. It rejects dispatch/policy drift before starting an installed bounded child. Credentials and host paths stay outside workflow arguments; only the complete policy fingerprint and routing/budgets enter history.

The fixture workflow owns a long-lived heartbeating serve activity, validates authenticated readiness, executes existing independent scenarios, durably exports the complete outbox before a fixed settlement cutoff, waits for window end, then obtains assessment. Its cleanup waits for activity cancellation to terminate the owned process group. A child watchdog handles parent death; a hard lifetime bounds orphan risk. Filesystem locks and the persisted per-run controller state prevent duplicate local effects on same-host retries. Temporal's [activity cancellation contract](https://github.com/temporalio/documentation/blob/main/docs/develop/python/workflows/cancellation.mdx) requires heartbeats and explicit wait-for-completion handling; these are tested with actual subprocesses.

For managed plans, the engineering workflow waits for assessment and target cleanup before creating telemetry collection children. Failure blocks collection and certification. Existing plans without `demo` retain their original command sequence and fixture identity. No workflow start is implicit and no cloud deployment authority is added.

Export completion is a private durable receipt over run/policy/outbox/time. Per-batch acknowledgements survive retries; lost backend acknowledgements remain at-least-once. New exports cannot cross the cutoff. A cached receipt remains usable only under current context/generation and the existing grace period. Ingestion acknowledgements do not assert visibility or replace independent telemetry verification.

The approved recipe remains exactly one packaged source edit, not arbitrary worker code or deployed-image provenance. One host and one dedicated lifecycle queue own persistent state. Independent runner/assessor/collector/verifier roles remain separate. General environment provisioning, multi-service target orchestration, cross-host failover and live backend acceptance remain outside this increment. See [the operator guide](../managed-demo-pilot.md).
