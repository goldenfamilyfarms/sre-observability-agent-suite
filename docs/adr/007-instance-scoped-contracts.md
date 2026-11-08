# ADR-007: explicit instance and generation identity

Status: accepted for Phase 1 increment 1. Date: 2026-11-07.

Context: environment class and source revision cannot distinguish concurrent previews or successive deployments of the same commit. Reusing such evidence or approval could satisfy the wrong environment's quality gate.

Decision: require environment ID and positive generation in every Scope, including stable environments. Derive preview IDs from GitHub repository ID and PR number. Carry scope through memory, evidence, worker context, checkpoints, release/approval contracts and version-2 certification reports. Use an optimistic persistent generation registry with atomic events and closure tombstones. Explicitly archive unscoped legacy data instead of fabricating attribution.

Alternatives: optional fields/default IDs preserve API compatibility but silently retain ambiguity; source SHA alone misses rebuilds and multiple PR instances; namespace names alone omit lifecycle generation; automatically relabeling old records invents provenance.

Consequences: package 0.2.0 breaks the old constructors and requires a deliberate local storage upgrade. Valid approval and evidence must match both ID and generation. Old database rows remain recoverable but cannot automatically enter trusted context. Local byte verification and current-generation checks are foundations for an authenticated service, not deployment permission or distributed transaction guarantees.

See [Phase 1 implementation and migration](../phase-1-foundation.md) for supported behavior, validation and remaining work.
