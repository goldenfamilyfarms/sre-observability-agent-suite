# ADR-014: Explicit loopback-only pilot lifecycle

Status: accepted for local Phase 1 acceptance; production hosting is excluded.

## Context

The execution engine and pilot setup command need running persistence, orchestration, authenticated HTTPS and a worker. Manual service wiring makes it difficult to repeat a scoped pilot or distinguish infrastructure health from authorization to execute a ticket. A containerized worker would also require access to host Git, Claude and the Docker test boundary.

## Decision

Provide a POSIX foreground supervisor and an explicit private initialization command. Run a digest-pinned PostgreSQL 16/17 container with a retained labeled volume, the installed Temporal development CLI with retained SQLite state, a loopback TLS reference API, and an optional host-side worker. Apply existing checksummed migrations after database readiness and before API startup. Require distinct unprivileged loopback ports, a private directory outside source control, explicit image availability and a separate worker JSON configuration. Do not source shell configuration or install binaries during startup.

Initialization authorizes the selected manifest's exact non-production scope, minting independent controller/worker/database secrets and a local TLS key/certificate. The controller only registers previews; worker permissions remain evidence write/read and context read. Runtime processes receive explicit environment allowlists. The worker receives neither database nor controller secrets. The API reloads digest-only credential configuration per request and fails closed on invalid changes.

Keep workflow execution separate from service startup. Expose the existing execution CLI through a restricted wrapper with local connection settings. Restrict additional certificate trust and nonstandard HTTPS ports to literal loopback endpoints, preserve normal remote TLS rules, and bind local trust-anchor content into runtime policy identity.

## Consequences

The process supervisor owns only its spawned process groups and its newly started labeled database container. Normal shutdown preserves state; no implicit data reset or prune exists. Startup conflicts and uncertain leftovers require explicit operator reconciliation. Tests prove retained SQL evidence and a Temporal workflow identity across stop/start, not host-loss recovery or cross-version upgrades. Temporal development RPC and local filesystem administration remain trusted-host boundaries.

The local WSGI reference host and Temporal development server are unsuitable for production. Seven-day self-signed certificates and scoped grants require explicit operator lifecycle management. No system trust-store modification, cloud resource creation, live Jira/model acceptance, shipping or production authorization is introduced.
