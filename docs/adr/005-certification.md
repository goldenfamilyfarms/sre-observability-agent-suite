# ADR-005: accountable final certification

Status: accepted; query adapters pending. Date: 2026-11-07.

Decision: version the catalog and report. Final service certification accounts for exactly 120 unique slots: 40 tracing, 40 logging and 40 PromQL. Ticket verification may use a subset but cannot claim final certification. Applicable mandatory checks must pass deterministic assertions. Not-applicable results require evidence and justification. ERROR and INCONCLUSIVE cannot pass certification.

Bind results to the exact service, environment, source commit, artifact, plan, catalog and observation window. A successful query response alone is not a PASS. Missing/duplicate/unknown results, stale evidence, unimplemented checks and scope mismatch produce INCOMPLETE.

Alternatives: counting successful HTTP responses would certify empty or irrelevant telemetry. Silent skipping conceals coverage gaps. A single unversioned checklist cannot support reproducibility.

Consequences: the initial catalog is a 120-slot design inventory, with null query templates and assertions. It cannot issue passing certificates. Synthetic populated definitions exist only in tests to exercise aggregation. Implement adapters against fixture-backed queries and the selected [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/) version; pin that version when instrumenting an actual target.
