# ADR-006: dynamic preview environments through gated GitOps

Status: proposed; requires an authorized pilot. Date: 2026-11-07.

Context: the supplied deployment notes describe GitHub, Helm, Argo CD and GKE. We need temporary environments for independent QA and telemetry verification without assuming access to any actual infrastructure.

Decision proposed: use a shared non-production GKE foundation and a namespace per preview, with isolation controls established before workloads. Build/test the exact PR source revision, authorize its immutable image digest, then publish desired state to a protected deployment repository consumed by an ApplicationSet Git file generator. Argo reconciles Kubernetes resources; the durable orchestrator owns authorization, lifecycle, resource inventory and external cleanup.

Alternatives: a direct PR generator is simpler and supports label-based discovery, but a label is not release authorization and it may observe a commit before its image exists. A cluster/project per preview provides a stronger boundary at greater cost and setup latency; select it when workload trust or isolation requirements demand it. Long-lived shared staging is cheaper but cannot alone provide isolated concurrent test state.

Consequences: implement instance/generation identity across contracts before execution, plus protected configuration publishing, stale-build rejection, TTL cleanup, external-resource ownership and scoped data/identity/network controls. Chart and destination policy remain platform-controlled. Production promotion still requires explicit human approval under ADR-004. No credentials, actual infrastructure inventory, deployable platform or automatic authorization are introduced by this ADR.

See the [dynamic-environment guide](../dynamic-environments.md) for wiring, lifecycle, isolation, teardown, source references and pilot acceptance evidence.
