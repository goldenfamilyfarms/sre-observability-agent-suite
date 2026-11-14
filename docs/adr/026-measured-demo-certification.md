# ADR 026: Measured demo instrumentation and infrastructure health evidence

Status: Accepted for the source-candidate local pilot.

The complete real-backend workflow exposed one failed exception-stack check and 21 inconclusive process, scrape, exporter, retention and SLI checks. Invented measurements or capability exemptions would undermine certification.

Add bounded real exception frames, actual process metrics and transactional export acknowledgement accounting. Audit local outbox contents against committed events; preserve immutable at-least-once retries and all generation/context fences. Evaluate reviewed finite-journey calculations in Prometheus, retaining all seven binding labels. Probe Loki's effective configuration through its authenticated local gateway and enable actual compactor retention.

Catalog 1.1 explicitly requires bound Prometheus observations for trace delivery, log ingestion acknowledgement latency and log retention configuration. Signal grouping does not imply these infrastructure concerns can be proved from application trace/log attributes. Existing private policies and plans must be regenerated; no evidence is relabeled or upgraded implicitly. Compare sample freshness at each evaluation time.

Provide the demo mappings as an explicit offline preparation option. Independent fixture capability decisions remain unchanged. Validate the complete real workflow plus injected missing/incorrect measurements, identity substitution, generation revocation, durable acknowledgement failures and replay.

A passing synthetic demo verifies its reviewed measurements and calculations. It does not prove production SLO attainment, remote durability, elapsed retention deletion, live Jira/model integration, or deployed artifact provenance. See [the instrumentation guide](../demo-instrumentation.md).
