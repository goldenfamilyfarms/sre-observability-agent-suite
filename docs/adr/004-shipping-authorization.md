# ADR-004: explicit release authorization

Status: accepted. Date: 2026-11-07.

Decision: default-deny all promotion. An explicit authorization may allow automatic progression of an exact non-production release. Production additionally requires an authenticated human approver authorized by policy. Approval binds service, environment, commit, immutable artifact, release-plan digest, required gates and expiry. Reject revoked, future, expired or mismatched approval.

The deployment adapter must independently verify CI/QA/certification evidence, current policy, branch protections, environment controls and rollback readiness immediately before acting. Only trusted services may create approval records. The local `Approval` dataclass is a policy input, not proof of a human interaction or signature.

Alternatives: environment-wide opt-in permits artifact substitution; approval in free-form chat alone is ambiguous; an agent approving its own change defeats separation of duties.

Consequences: changing the artifact, commit, plan or gate set requires new authorization. Policy tests exercise denial and exact-match success. This PR has no merge or deployment executor and grants no runtime promotion authorization.
