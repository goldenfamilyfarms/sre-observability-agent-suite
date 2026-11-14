# ADR-020: require current certification at engineering review readiness

Status: accepted for the opt-in execution pilot; live acceptance pending. Date: 2026-11-14.

Decision: bind private certification configuration into the engineering manifest, freeze an exportable certification plan alongside the existing candidate-bound telemetry plan, and dispatch the existing ServiceCertification workflow on its separate queue after observations and independent fixture evidence are available. Authentication comes from evidence API producer identity and bytes, not the Temporal signal. A bounded, first-reference-only signal supplies fixture metadata; it cannot change policy, source mappings, release or plan. Save the exact child input for durable retries.

The final activity resolves the authenticated certificate using the current gate reader, revalidates the exact saved engineering candidate/manifest, observations, scope, context and evidence, and repeats the gate around publication side effects. Failure or missing certification cannot become readiness. A previously published provisional draft is retained; no new shipping authority follows from certification.

A separate workflow preserves verifier credential isolation and process deadlines. Recorded prepare output keeps old histories on the old path; a changed runtime policy cannot reuse their immutable manifests. Parent restart, history replay, lost input/report acknowledgements, bounded fixture waits and parent/child cancellation have explicit tests. Child cancellation follows the [Temporal child workflow API](https://docs.temporal.io/develop/python/workflows/child-workflows).

Alternatives: trusting a PASSED string omits current-generation and artifact checks. Letting the engineering worker author its own fixture removes independent assessment. Reusing an arbitrary standalone certificate disconnects the final gate from the actual engineering observations. An unbounded signal wait hides missing assessor/worker setup.

Consequences: an operator must configure the separate verification worker and independent assessor. The engineering collector's current source/step/limit constraints remain explicit and are validated against certification policy. Standalone verification can support more sources. Automated fixture traffic, live target acceptance, deployed-image provenance, shipping and production approval governance remain separate work. See the [operator guide](../engineering-certification.md).
