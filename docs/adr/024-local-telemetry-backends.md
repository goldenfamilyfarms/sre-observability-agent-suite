# ADR 024: Explicit local telemetry backends and real pilot acceptance

Status: Accepted for the single-host non-production pilot.

## Context

Managed fixture tests established durable export against local protocol sinks. They could not prove Prometheus scraping, Loki query visibility, full Tempo retrieval or backend persistence. The existing collectors require authenticated HTTPS and exact candidate/scope evidence; a local test stack must preserve those contracts.

## Decision

Provide `sre_suite.local_telemetry` with private preparation, explicit image pulls, authorized foreground startup and ownership-checked stop. Pin Prometheus/Loki/Tempo image digests and generated configurations. Persist each backend's data in a private bind mount. Use loopback listeners with Linux native host networking or macOS Docker Desktop port publication. Prometheus scrapes the exact managed HTTPS target with its own metrics credential and target CA.

Expose fixed read/ingest routes through loopback HTTPS gateways with five separate credentials. Add an explicit loopback-only `SRE_TELEMETRY_CA_FILE` profile option; preserve legacy serialization when it is absent. Retain query/response artifact, plan, scope, generation, window and authenticated evidence-context validation. Prepare reviewed private policy changes before computing immutable dispatch fingerprints. Startup never starts a workflow.

Test the actual managed Temporal fixture, scrape/export/query/evidence path, persisted restart and rejection boundaries against all three backends in CI. Preserve the existing independent fixture and 120-check certification gates. A narrow generated query policy cannot substitute for the full reviewed verification policy or certify the demo by query success alone.

## Consequences

The local loopback/host/Docker administrator is trusted; backend access is not per-preview multi-tenant isolation. This pilot adds no cloud, merge, promotion or deployment executor. Single-host persistence is tested, but cross-host failover and host disaster recovery are not. Image upgrades, certificate rotation, disk lifecycle and complete service-specific certification acceptance require explicit operator review. See [the operator guide](../local-telemetry.md) for setup, limits and recovery.
