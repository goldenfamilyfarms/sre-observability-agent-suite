"""Private per-occurrence capture journal and retry-safe authenticated evidence publication."""

import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from ..api_wire import decode, fields, text
from ..execution.journal import Journal
from ..execution.wire import digest
from .collector import TelemetryCollector
from .contracts import integer
from .schedule_contracts import TelemetrySchedule


class ScheduledRuntime:
    def __init__(self, collector: TelemetryCollector, journal: Journal) -> None:
        self.collector = collector
        self.journal = journal

    def collect(self, value: object) -> dict[str, Any]:
        raw = fields(value, {"schedule", "slot"})
        schedule = TelemetrySchedule.parse(raw["schedule"])
        slot = integer(raw["slot"], 0, schedule.occurrences - 1)
        schedule.authorize(self.collector.policy)
        request = schedule.runnable(slot, self.collector.clock())
        api = self.collector.evidence
        api.context(request.scope)  # Always validate, even when returning saved captures.
        key = schedule.workflow_id + ":" + str(slot)

        def capture() -> dict[str, Any]:
            result = self.collector.collect(request)
            return {"content": result.content.decode("utf-8")}

        saved = self.journal.compute(key + ":capture", value, capture)
        content = text(saved["content"], 900_000).encode()
        document = decode(content)
        if not isinstance(document, dict) or (
            digest(document.get("request")) != digest(asdict(request))
            or digest(document.get("profile"))
            != digest(asdict(self.collector.policy.resolve(request)))
            or document.get("certification_verdict") is not None
        ):
            raise ValueError("saved observation does not match this schedule")
        version = integer(document.get("context_version"), 0, 2**63 - 1)
        observed = datetime.fromisoformat(text(document.get("observed_at")))
        expires = observed + timedelta(minutes=15)
        if not observed <= self.collector.clock() < expires:
            raise ValueError("saved observation expired; create a new reviewed schedule")
        schedule.runnable(slot, self.collector.clock())
        api.context(request.scope, version)
        evidence_id = "telemetry-" + hashlib.sha256(content).hexdigest()
        # Retrying an acknowledged-or-lost upload reuses the exact ID, content and times.
        evidence = api.upload(evidence_id, request.scope, content, observed, expires)
        api.verify(evidence)
        api.context(request.scope, version)
        result = document.get("result")
        if not isinstance(result, dict) or result.get("status") not in {
            "observed",
            "empty",
            "partial",
        }:
            raise ValueError("invalid saved result")
        receipt = {
            "slot": slot,
            "status": result["status"],
            "evidence_id": evidence.id,
            "evidence_sha256": evidence.sha256,
        }
        self.journal.compute(key + ":receipt", {"content_sha256": evidence.sha256}, lambda: receipt)
        # Only compact references enter Temporal history. Query/trace bodies stay private.
        return receipt
