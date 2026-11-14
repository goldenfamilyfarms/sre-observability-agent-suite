# ADR 028: Authenticated suite agents and bounded release operations

Status: Implemented for the bounded v1 adapters; live provider acceptance pending.

## Decision

Keep Temporal as the durable controller. Independent engineering subjects and queues bind each stage to preceding evidence hashes. Keep approvals, proposed knowledge, reconciliation, audit, immutable artifact references and acknowledged event batches in the shared transactional control API. The server determines human and steward authority; requests cannot declare themselves human. PostgreSQL migration 003 upgrades knowledge states without discarding existing records.

Use a frozen, explicitly approved suite graph for dependency dispatch and at most two separately reviewed corrections. Do not edit a running workflow's source scope. Epic assignment, knowledge reconciliation and source advancement are checkpoints that require a new reviewed plan. Child agents still enforce their own source, artifact, plan, context and generation boundaries. Model success and process exit are never service acceptance.

Shipping validates the whole Git candidate tree, trusted CI and independent review, then uses an ancestor-checked atomic ref lease to fast-forward the exact tested head. Delivery validates an independently attested image, the pinned chart recipe and GKE cluster identity before managing owned namespaces and Argo Applications through Kubernetes. Resource replacement/deletion uses UID/resourceVersion fences. Rollback/cleanup need their own exact approvals. Only a container-image certificate can authorize final image promotion; preview readiness is a distinct outcome.

Use deterministic source standards, AST citations, parent/span topology, predicate-based pipeline health and complete-catalog regression comparisons to support assessment/remediation. Generic HTTP journeys have fixed reviewed origins/routes and separate runner credentials. Persist POST intent before execution and refuse blind repetition after an uncertain outcome. The fixture assessor's oracle stays independent of returned telemetry.

## Consequences and limits

Private infrastructure provides an isolated trusted image builder, registry, GKE cluster, Argo installation, TLS, scoped provider credentials and service assertions. Execution journals require durable shared POSIX storage for multi-host recovery. The control service's PostgreSQL state does not magically replicate local worker files. Default configuration contains no production grants or broad automatic promotion authorization.

This change implements the proposal's twelve role entrypoints; it does not claim universal framework coverage, live organizational acceptance, cross-repository workstream scheduling, or completed backup/restore/load/upgrade operations. The suite graph is scoped to one source repository and source scope. Cross-repository workstreams require separately authorized graphs. The planning agent uses deterministic evidence-derived decomposition; model-assisted plan decomposition is an optional future extension, not a substitute for provenance checks.

See [operator guide](../agent-suite-operations.md) for contracts and private setup, and [completion ledger](../suite-completion.md) for explicit acceptance boundaries.
