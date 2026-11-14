"""Private policy and separate runner/assessor environments."""

from collections.abc import Mapping
from pathlib import Path

from ..execution.http import JsonHttp
from ..verification.config import environment as evidence_environment
from ..verification.config import private_document
from .contracts import FixturePolicy


def load_policy(path: Path) -> FixturePolicy:
    return FixturePolicy.parse(private_document(path, 1_048_576))


def environment(policy: FixturePolicy, role: str, env: Mapping[str, str]) -> dict[str, str]:
    if role not in {"runner", "assessor"}:
        raise ValueError("invalid fixture role")
    result = evidence_environment(env)
    if role == "runner":
        names = [policy.authorization_env, policy.ca_env]
        if policy.journeys:
            for origin in policy.journeys["origins"].values():
                names.extend([origin["authorization_env"], origin["ca_env"]])
        if policy.journey_export:
            names.extend(
                [policy.journey_export["authorization_env"], policy.journey_export["ca_env"]]
            )
        for name in names:
            if name and name in env:
                result[name] = env[name]
    return result


def target(policy: FixturePolicy, env: Mapping[str, str]) -> JsonHttp:
    authorization = env[policy.authorization_env]
    if not authorization or any(ord(c) < 32 or ord(c) > 126 for c in authorization):
        raise ValueError("invalid fixture target credential")
    return JsonHttp(
        policy.base_url,
        authorization,
        timeout=10,
        local_ca_file=env[policy.ca_env] if policy.ca_env else None,
    )
