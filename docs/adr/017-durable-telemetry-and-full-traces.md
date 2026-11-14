# ADR-017: full trace evidence and finite durable collection

Status: accepted for the telemetry pilot; credentialed acceptance pending. Date: 2026-11-14.

Decision: add a separate operator-reviewed full-trace permission and Tempo V2 reader that preserves bounded OTLP data. Validate every span's trace identity and require an in-window span from a resource matching all five scope dimensions. Preserve cross-service context with explicit per-span attribution. Surface partial/pruned/dropped/disconnected results without inventing certification success.

Use a dedicated Temporal workflow for finite, serialized collection occurrences with durable timers, immutable policy fingerprints, fixed windows, bounded retries and explicit start authorization. A private per-occurrence journal saves capture bytes before idempotent evidence publication; reuse rechecks preview generation, knowledge version and expiry. The workflow records only metadata and evidence references. A killable subprocess supplies the total deadline missing from socket timeouts, with heartbeat-driven cancellation and no unrelated service credentials.

Alternatives: thread-only HTTP timeouts cannot reliably stop a trickling/blocking socket; re-querying on every lost acknowledgement changes evidence and observation time; a perpetual unbounded schedule risks outliving preview authority; treating search summaries or a backend COMPLETE enum as final certification hides missing telemetry. These are deliberately avoided by the implemented contracts.

Consequences: an installed package and dedicated worker/journal are required. Operators still configure backend/namespace permissions privately. Scope hashes do not authenticate humans or instrumentation truth. Evidence already committed cannot be rolled back by cancellation. A crash before saving a capture may repeat a source read. Cross-host journal recovery and live backend/Temporal acceptance remain open, as do deterministic 120-check certification, artifact/plan binding, and separately authorized cloud deployment agents.

See the [operator guide](../telemetry-scheduling.md), [workflow](../../sre_suite/telemetry/temporal_workflow.py), [private runtime](../../sre_suite/telemetry/scheduled_runtime.py) and [engine tests](../../orchestration_tests/test_telemetry.py).

Follow-up: [ADR-018](018-engineering-telemetry-binding.md) adds optional engineering child collection and source-candidate/plan attribution in 0.15. Deterministic certification and deployed-image provenance remain separate.
