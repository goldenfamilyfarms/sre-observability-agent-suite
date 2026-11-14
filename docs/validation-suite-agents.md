# Governed suite agents validation

Package 0.25.0 adds bounded implementations for the remaining proposal roles. See the [implementation ledger](suite-completion.md) for exact scope and remaining roadmap work, and the [operator guide](agent-suite-operations.md) for private configuration.

Local validation on Python 3.13.14:

| Check | Result |
| --- | --- |
| Default unit suite | 768 passed; subsequently added image-certificate provenance test also passed |
| Real loopback HTTPS protocol tests | 19 passed |
| Real PostgreSQL integration, migration 003 and control API | 41 passed |
| Real Temporal replay, recovery, cancellation and container integration | 61 passed |
| Real local pilot startup, worker readiness, persistent restart and TLS rejection | 2 passed |
| Strict mypy, Ruff and formatting | Passed |
| Local Git shipping transport | Included: exact fast-forward and concurrent-ref lease protection |
| Helm 4.2.3 rendering | Pinned recipe rendered and matched the expected Kubernetes resources |

The new control, suite runtime and analysis tests exercise real scoped evidence/control services with synthetic identities. Cloud/Jira/GitHub adapters use deterministic transport fixtures except the local Git transport tests. No credentialed Jira, Claude, GKE, Argo CD, registry or production operation was executed. Those acceptance tests belong in the private target environment.

CI additionally runs Python 3.11 and 3.14, PostgreSQL 16/17, local pilot startup and real Prometheus/Loki/Tempo pilot certification. Consult the PR checks for the result of the exact commit being reviewed. Successful tests do not authorize merging, workflow start or deployment.
