"""Fence telemetry reads and optional evidence capture with current scoped API context."""

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from ..contracts import Evidence
from ..execution.evidence_client import EvidenceClient
from ..execution.wire import canonical
from .adapters import ReadTransport, TelemetryAdapter
from .contracts import QueryPolicy, QueryProfile, QueryRequest, QueryResult


@dataclass(frozen=True)
class Collection:
    result: QueryResult
    content: bytes
    evidence: Evidence | None


class TelemetryCollector:
    def __init__(
        self,
        policy: QueryPolicy,
        evidence: EvidenceClient,
        connect: Callable[[QueryProfile], ReadTransport],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.policy = policy
        self.evidence = evidence
        self.connect = connect
        self.clock = clock

    def collect(self, request: QueryRequest, *, upload_evidence: bool = False) -> Collection:
        profile = self.policy.resolve(request)
        request.current(self.clock())
        context = self.evidence.context(request.scope)
        version = context["version"]
        result = TelemetryAdapter(profile, self.connect(profile)).query(request)
        self.evidence.context(request.scope, version)
        observed = self.clock()
        request.current(observed)
        document = {
            "schema_version": 2,
            "kind": "telemetry-observation",
            "request": asdict(request),
            "profile": asdict(profile),
            "rendered_query": profile.query(request.scope),
            "context_version": version,
            "observed_at": observed.isoformat(),
            "result": asdict(result),
            "certification_verdict": None,
            "operation": "trace-by-id" if request.trace_id else "query",
            "attribution": (
                "matching-resource-spans-with-cross-service-context"
                if result.full_trace
                else (
                    "query-policy-only"
                    if result.backend.value == "tempo"
                    else "query-policy-and-returned-labels"
                )
            ),
        }
        content = (canonical(document) + "\n").encode()
        if len(content) > 1_000_000:
            raise ValueError("observation exceeds evidence budget")
        reference = None
        if upload_evidence:
            self.evidence.context(request.scope, version)
            key = "telemetry-" + hashlib.sha256(content).hexdigest()
            reference = self.evidence.upload(
                key, request.scope, content, observed, observed + timedelta(minutes=15)
            )
            self.evidence.verify(reference)
            self.evidence.context(request.scope, version)
        return Collection(result, content, reference)
