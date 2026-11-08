"""Fail-closed decision function, not an identity or deployment service."""

from dataclasses import dataclass
from datetime import datetime

from .contracts import Approval, Environment, GateResult, Release, aware


@dataclass(frozen=True)
class PromotionPolicy:
    # Exact releases are authorized, never environment-wide wildcards.
    authorized_nonproduction: tuple[Release, ...] = ()
    production_approvers: frozenset[str] = frozenset()


def promotion_denials(
    release: Release,
    gates: tuple[GateResult, ...],
    policy: PromotionPolicy,
    now: datetime,
    approval: Approval | None = None,
) -> tuple[str, ...]:
    aware(now)
    reasons: list[str] = []
    if len({gate.gate for gate in gates}) != len(gates):
        reasons.append("duplicate gate results")
    for name in release.required_gates:
        matching = [gate for gate in gates if gate.gate == name]
        if len(matching) != 1 or not (
            matching[0].passed is True
            and matching[0].release == release
            and matching[0].evidence.valid_for(release.scope, now)
        ):
            reasons.append(f"missing, failed, stale, or mismatched gate: {name}")
    if release.scope.environment is Environment.PRODUCTION:
        if approval is None or not (
            approval.release == release
            and approval.approver in policy.production_approvers
            and approval.issued_at <= now < approval.expires_at
            and not approval.revoked
        ):
            reasons.append("valid explicit human production approval required")
    elif release not in policy.authorized_nonproduction:
        reasons.append("this non-production release is not explicitly authorized")
    return tuple(reasons)
