"""Synthetic independent assessor/collector/verifier and representative predicate fixtures."""

import copy
from dataclasses import asdict, replace
from datetime import timedelta

from sre_suite.auth import TokenAuthenticator
from sre_suite.certification import load_catalog
from sre_suite.contracts import Release
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical, digest
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import Backend, QueryPolicy, epoch_ns
from sre_suite.verification.contracts import VerificationInput, VerificationPolicy, catalog_digest
from sre_suite.verification.runtime import CertificationEngine
from tests.execution_fixture import Fixture
from tests.test_api import AUDIENCE, SECOND_TOKEN, credential, request
from tests.test_engineering_telemetry import BINDINGS
from tests.test_foundation import NOW
from tests.test_telemetry import Source, profile

TRACE = "a" * 32
ROOT = "f" * 16
THIRD = "synthetic-verifier-token-" + "z" * 32
START, END = NOW - timedelta(seconds=120), NOW - timedelta(seconds=1)


def case(check):
    op = check.assertion.removesuffix(".v1")
    category = check.category.value
    base = "/attributes" if category == "traces" else "/body" if category == "logs" else "/labels"
    path = base + "/value"
    index = int(check.id.split("-")[1])
    row = {
        "attributes": {"value": "ok", "case": check.id},
        "body": {"value": "ok", "case": check.id},
        "labels": {"value": "ok", "case": check.id},
        "_in_scope": True,
        "trace_id": TRACE,
        "span_id": f"{index:016x}",
        "parent_span_id": ROOT,
        "start_ns": epoch_ns(START) + 1_000_000_000,
        "end_ns": epoch_ns(END) - 1_000_000_000,
        "timestamp_ns": epoch_ns(END) - 1,
        "duration_ns": 1,
        "events": [{"name": "exception", "attributes": {"exception.type": "SyntheticError"}}],
        "links": [{"trace_id": TRACE, "span_id": ROOT}],
        "samples": [
            {"timestamp_ns": epoch_ns(START), "value": 1.0},
            {"timestamp_ns": epoch_ns(END), "value": 1.0},
        ],
    }
    parent = {
        **row,
        "span_id": ROOT,
        "parent_span_id": None,
        "start_ns": epoch_ns(START),
        "end_ns": epoch_ns(END),
    }
    p, expected, rows = {}, [], [row]
    value = "ok"
    if op in {"present", "unique", "coverage", "timestamp", "unicode", "control_chars"}:
        p = {"path": path}
    if op in {"enum", "bounded"}:
        p = {"path": path, "values": ["ok"]}
        if op == "bounded":
            p["max_length"] = 8
    if op == "range":
        p, value = {"path": path, "minimum": 0, "maximum": 2}, 1
    if op == "equals":
        p = {"path": path, "value": "ok"}
    if op == "coverage":
        expected = ["ok"]
    if op == "excluded":
        p = {"paths": [path], "forbidden": ["synthetic-secret"]}
    if op == "cardinality":
        p = {"path": path, "maximum": 1}
    if op == "hex_id":
        p, value = {"path": path, "length": 32}, TRACE
    if op == "timestamp":
        value = START.isoformat()
    if op == "latency":
        p, value = {"path": path, "maximum_seconds": 120}, START.isoformat()
    if op == "event":
        p = {"name": "exception", "attributes": ["exception.type"]}
    if op == "correlation":
        p = {"trace_path": "/body/trace_id", "span_path": "/body/span_id"}
        row["body"].update(trace_id=TRACE, span_id=ROOT)
    if op == "relation":
        p = {"parent": {"/span_id": ROOT}, "child": {"/span_id": row["span_id"]}}
    if op == "links":
        expected = [{"trace_id": TRACE, "span_id": ROOT}]
    if op == "timing":
        p = {"maximum_clock_skew_ns": 0}
    if op == "mapping":
        p = {
            "source_path": base + "/source",
            "target_path": path,
            "pairs": [{"source": "error", "target": "ERROR"}],
        }
        value = "ERROR"
        for container in ("attributes", "body", "labels"):
            row[container]["source"] = "error"
    if op == "metric_range":
        p = {"minimum": 0, "maximum": 2, "minimum_samples": 2}
    if op == "metric_expected":
        p, expected = {"tolerance": 0.001, "minimum_samples": 2}, [1.0]
    if op == "available":
        p = {"minimum_samples": 2}
    if op == "sample_age":
        p = {"maximum_seconds": 10, "minimum_samples": 2}
        for s in row["samples"]:
            s["value"] = END.timestamp() - 1
    if op == "monotonic":
        p = {"maximum_resets": 0, "minimum_samples": 2}
    for container in ("attributes", "body", "labels"):
        row[container]["value"] = value
    if op == "buckets":
        p = {"bounds": ["0.5", "+Inf"], "minimum_samples": 2}
        row["labels"]["le"] = "0.5"
        second = copy.deepcopy(row)
        second["labels"]["le"] = "+Inf"
        for s in second["samples"]:
            s["value"] = 2
        rows.append(second)
    return p, rows, expected, [parent, *rows]


def negative(check, p, rows, expected, context):
    op = check.assertion.removesuffix(".v1")
    rows, context = copy.deepcopy(rows), copy.deepcopy(context)
    if "path" in p:
        parts = p["path"].split("/")[1:]
        target = rows[0]
        for part in parts[:-1]:
            target = target[part]
        target.pop(parts[-1], None)
    elif op == "excluded":
        rows[0][p["paths"][0].split("/")[1]]["value"] = "synthetic-secret"
    elif op == "mapping":
        rows[0][p["source_path"].split("/")[1]].pop("source")
    elif op == "structured":
        rows[0]["body"] = None
    elif op == "event":
        rows[0]["events"] = []
    elif op == "correlation":
        rows[0]["body"]["trace_id"] = "b" * 32
    elif op == "links":
        rows[0]["links"] = []
    elif op == "relation":
        rows[0]["parent_span_id"] = "e" * 16
    elif op == "roots":
        context[0]["parent_span_id"] = "e" * 16
    elif op == "graph":
        rows[0]["parent_span_id"] = "e" * 16
    elif op == "timing":
        rows[0]["end_ns"] = rows[0]["start_ns"] - 1
    elif op == "buckets":
        rows = rows[:-1]
    elif op == "monotonic":
        rows[0]["samples"][0]["value"] = 2
    else:
        rows[0]["samples"] = []
    return rows, expected, context


class TokenTransport:
    def __init__(self, app, token):
        self.app, self.token = app, token

    def call(self, method, path, payload=None):
        status, body, _ = request(self.app, path, payload, token=self.token)
        if status != 200:
            raise RemoteError(status)
        return body


def otlp_attributes(values):
    result = []
    for k, v in values.items():
        kind = (
            "intValue" if type(v) is int else "doubleValue" if type(v) is float else "stringValue"
        )
        result.append({"key": k, "value": {kind: str(v) if kind == "intValue" else v}})
    return result


class VerificationFixture:
    def __init__(self, root):
        self.fixture = Fixture(root)
        self.scope = self.fixture.ticket.scope
        self.fixture.app._auth = TokenAuthenticator(
            AUDIENCE,
            (
                credential(self.scope),
                credential(self.scope, token=SECOND_TOKEN, subject="assessor"),
                credential(self.scope, token=THIRD, subject="verifier"),
            ),
        )
        self.api = EvidenceClient(
            "https://evidence.example.invalid", THIRD, TokenTransport(self.fixture.app, THIRD)
        )
        self.assessor = EvidenceClient(
            "https://evidence.example.invalid",
            SECOND_TOKEN,
            TokenTransport(self.fixture.app, SECOND_TOKEN),
        )
        self.catalog = load_catalog()
        self.release = Release(
            self.scope, "sha256:" + "c" * 64, "sha256:" + "d" * 64, ("certification",)
        )
        profiles = [
            replace(profile(b), binding_labels=BINDINGS, allow_full_traces=b == Backend.TEMPO)
            for b in Backend
        ]
        self.policy_raw = {
            "schema_version": 2,
            "scope": asdict(self.scope),
            "collector": "worker-one",
            "assessor": "assessor",
            "verifier": "verifier",
            "catalog_sha256": catalog_digest(self.catalog),
            "sources": [
                {
                    "profile": {
                        **asdict(p),
                        "scope_labels": dict(p.scope_labels),
                        "binding_labels": dict(p.binding_labels),
                    },
                    "trace_id": TRACE if p.backend == Backend.TEMPO else None,
                    "step_seconds": 15,
                    "limit": 100,
                }
                for p in profiles
            ],
            "checks": {},
        }
        self.populate()
        self.policy = VerificationPolicy.parse(self.policy_raw)
        self.collect()

    def populate(self):
        self.assessments = {}
        self.rows = {category: [] for category in ("traces", "logs", "promql")}
        for check in self.catalog.checks:
            params, rows, expected, context = case(check)
            category = check.category.value
            container = (
                "attributes" if category == "traces" else "body" if category == "logs" else "labels"
            )
            self.rows[category].extend(rows)
            self.policy_raw["checks"][check.id] = {
                "applicable": True,
                "source": {"traces": "tempo", "logs": "loki", "promql": "prometheus"}[category],
                "selector": {f"/{container}/case": check.id},
                "parameters": params,
                "minimum_records": 1,
            }
            self.assessments[check.id] = {
                "applicable": True,
                "scenario": "synthetic " + check.id,
                "expected": expected,
                "justification": "",
            }
        self.rows["traces"].insert(0, {**context[0], "attributes": {}, "events": [], "links": []})

    def collect(self):
        self.observations = []
        for source in self.policy.sources:
            req = source.request(self.release, START, END)
            p = source.profile
            labels = p.bindings(self.scope, req.binding)
            if p.backend == Backend.PROMETHEUS:
                raw = {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {**r["labels"], **labels},
                                "values": [
                                    [s["timestamp_ns"] / 1e9, str(s["value"])] for s in r["samples"]
                                ],
                            }
                            for r in self.rows["promql"]
                        ],
                    },
                }
            elif p.backend == Backend.LOKI:
                raw = {
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {**labels, "case": r["body"]["case"]},
                                "values": [[str(r["timestamp_ns"]), canonical(r["body"])]],
                            }
                            for r in self.rows["logs"]
                        ],
                    },
                }
            else:
                spans = []
                for r in self.rows["traces"]:
                    spans.append(
                        {
                            "traceId": TRACE,
                            "spanId": r["span_id"],
                            "parentSpanId": r["parent_span_id"] or "",
                            "name": "synthetic",
                            "startTimeUnixNano": str(r["start_ns"]),
                            "endTimeUnixNano": str(r["end_ns"]),
                            "attributes": otlp_attributes(r["attributes"]),
                            "events": [
                                {
                                    "name": e["name"],
                                    "timeUnixNano": str(r["start_ns"]),
                                    "attributes": otlp_attributes(e["attributes"]),
                                }
                                for e in r["events"]
                            ],
                            "links": [
                                {"traceId": link["trace_id"], "spanId": link["span_id"]}
                                for link in r["links"]
                            ],
                        }
                    )
                raw = {
                    "trace": {
                        "resourceSpans": [
                            {
                                "resource": {"attributes": otlp_attributes(labels)},
                                "scopeSpans": [{"spans": spans}],
                            }
                        ]
                    }
                }
            collector = TelemetryCollector(
                QueryPolicy(self.scope, (p,)),
                self.fixture.evidence,
                lambda p, raw=raw: Source(raw),
                clock=lambda: NOW,
            )
            self.observations.append((p.id, collector.collect(req, upload_evidence=True).evidence))
        self.input = self.make_input()
        self.engine = CertificationEngine(
            self.policy,
            self.api,
            Journal(self.fixture.root / "verification-journal"),
            clock=lambda: NOW,
        )

    def make_input(self):
        document = {
            "schema_version": 2,
            "kind": "certification-fixture",
            "release": asdict(self.release),
            "artifact_kind": "source-candidate-v1",
            "policy_sha256": self.policy.fingerprint,
            "catalog_sha256": self.policy.catalog_sha256,
            "context_version": 0,
            "window_start": START.isoformat(),
            "window_end": END.isoformat(),
            "checks": self.assessments,
        }
        content = canonical(document).encode()
        evidence = self.assessor.upload(
            "fixture-" + digest(document), self.scope, content, NOW, NOW + timedelta(minutes=15)
        )
        return VerificationInput(
            self.release,
            "source-candidate-v1",
            START,
            END,
            0,
            self.policy.fingerprint,
            evidence,
            tuple(self.observations),
        )

    def close(self):
        self.fixture.close()
