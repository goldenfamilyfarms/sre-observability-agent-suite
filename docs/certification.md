# Deterministic telemetry certification

Version 0.16 implements a parameterized 120-check engine: 40 trace, 40 log and 40 PromQL checks. Catalog `1.0.0` maps every stable ID to an executable, bounded predicate. The [catalog reference](check-catalog.md) lists every mapping and required parameter. The old `0.1.0-draft` catalog was a design inventory; it could not certify anything.

The engine consumes authenticated, previously collected observations. It does **not** invent service queries, run synthetic traffic, discover applicability, or infer thresholds from telemetry. Operators supply reviewed source profiles, field mappings, thresholds and independently produced fixture evidence privately. The shipped template is deliberately invalid until those decisions are made. A synthetic test passing all 120 checks is not a certification of a live service.

## Trust and scope

Three distinct evidence API subjects are required: `collector` for captured observations, `assessor` for fixture/applicability evidence, and `verifier` for published reports. The authenticated API controls producer identity; a string in a model response does not grant it. Give each credential only the exact preview scope/actions required. Keep all keys in private environment variables. The verifier needs context/evidence reads and report writes; its subprocess receives only evidence API/TLS credentials, never backend, Jira, GitHub or model credentials.

Inputs bind the complete service/environment/base revision/preview ID/generation, source-candidate artifact digest, engineering manifest plan digest, observation window, context version, catalog fingerprint and reviewed verification-policy fingerprint. The supported artifact kind is `source-candidate-v1`, as defined by the [engineering handoff](engineering-telemetry.md). This is not a deployed-image attestation. Verification policy has its own digest; changing assertions requires a newly reviewed policy, fixture and certification execution.

The backend document must match its exact reviewed profile, rendered query hash, trace ID where applicable, query limits/step, binding labels and window. The verifier reads actual bytes via the evidence API, checks producer/hash/freshness, and revalidates returned labels/full trace attributes. Search-only Tempo, partial/warning responses and empty results cannot pass. Full traces retain cross-service context for propagation/graph checks; asserted records must match the target scope and window. Conflicting versions of a span across sources block certification.

## Prepare a private policy

Install the core package; install `.[orchestration]` for Temporal. Create a physical owner-only directory outside Git. Files must be `0600`, directories `0700`, without symlink/hard-link aliases.

```sh
python -m sre_suite.verification template \
  --output /absolute/private/certification-policy.json
```

Fill its exact fields:

- `scope`: the existing five-dimension preview scope.
- `collector`, `assessor`, `verifier`: distinct authenticated API subjects.
- `catalog_sha256`: generated from the packaged catalog; review any change.
- `sources`: 1–120 entries, each containing `profile`, `trace_id`, `step_seconds`, `limit`. `profile` uses the existing [telemetry query profile](telemetry-adapters.md), including two artifact/plan `binding_labels`. Each source/profile ID must be unique. Tempo requires full-trace authorization and a known trace ID; non-Tempo uses null. Sources can share an existing observation across checks. Supply additional captured sources when three engineering observations cannot cover the suite.
- `checks`: exactly all 120 IDs, each with `applicable` (boolean), `source` (profile ID), `selector` (JSON-pointer/scalar equality filters), `parameters` (the exact predicate-specific fields), and `minimum_records` (1–5000). Predicate choice comes from the catalog, not from this file. Unknown parameters or IDs are rejected.

A source entry is shaped as follows; the endpoint and mapping are synthetic:

```json
{
  "profile": {
    "id": "scrape-availability", "backend": "prometheus",
    "base_url": "https://prometheus.example.invalid",
    "authorization_env": "SRE_TELEMETRY_PROMETHEUS_AUTH", "tenant_env": null,
    "query_template": "up${scope}",
    "scope_labels": {
      "service": "service_name", "environment": "deployment_environment",
      "revision": "service_version", "environment_id": "preview_id", "generation": "preview_generation"
    },
    "allow_full_traces": false,
    "binding_labels": {"artifact_digest": "artifact_digest", "plan_digest": "plan_digest"}
  },
  "trace_id": null, "step_seconds": 15, "limit": 100
}
```

For `PROMQL-009`, set `source` to that ID, `selector` to `{}`, `parameters` to `{"minimum_samples":2}`, and `minimum_records` to 1. The fixed `available.v1` predicate requires every selected sample to equal 1; successful `up=0` query results fail. This one example is insufficient to configure the full suite.

Selectors and predicate paths are bounded JSON pointers, not executable expressions. Canonical trace records expose `trace_id`, `span_id`, `parent_span_id`, `name`, string `kind`/`status`, `resource`, `attributes`, `events`, `links`, `start_ns`, `end_ns`, and `duration_ns`. Log records expose parsed JSON `body` (null on malformed/non-object records), `labels`, `metadata`, original `line`, and Loki `timestamp_ns`. Metric records expose `labels` and ordered finite `samples` with `timestamp_ns`/`value`. OTel dotted keys are literal keys: `/attributes/http.request.method`. Escape `/` in keys as `~1` and `~` as `~0`.

Review mappings against the check's named concern. A path to an unrelated constant, an excessively broad threshold, or an incorrect PromQL expression can weaken a policy even though it is syntactically valid. The engine authenticates and fingerprints reviewed configuration; it cannot decide whether a service owner chose a meaningful SLO. Server-side PromQL calculations use recorded output samples; `metric_expected` additionally compares them against an independent fixture oracle. `sample_age` expects **sample timestamp values** (for example from a reviewed `timestamp(...)` query), not range-query evaluation timestamps. All seven binding labels must survive query aggregation. Classic histogram checks require matching timestamp grids, reviewed ordered `le` buckets ending in `+Inf`, and cumulative nonnegative counts. Native histograms remain unsupported by the source adapter.

`excluded` checks configured literal canaries in selected text fields recursively, including decoded Unicode. It is not a universal secret/PII scanner. Expected-emission, sampling, retention, exporter, cross-service and journey checks require actual fixture/audit observations appropriate to that concern. A retention configuration assertion does not prove a long-term retention experiment. No alert routing or delivery is deployed or exercised by a severity assertion.

Validate offline and record the returned fingerprint:

```sh
python -m sre_suite.verification validate --policy /absolute/private/certification-policy.json
```

## Collect and assess the exact window

Use the existing bounded [telemetry scheduler](telemetry-scheduling.md) or engineering child collectors. Each request must carry the same candidate binding and fixed window, with the exact profile/trace ID/step/limit defined above. Populate the report input with the **server-returned evidence metadata**, not the client's upload key. The engine never treats a caller-supplied PASS as a check result.

The independent assessor uploads an evidence document with this shape through the authenticated API:

```json
{
  "schema_version": 2,
  "kind": "certification-fixture",
  "release": {"scope": "REPLACE_WITH_SCOPE_OBJECT", "artifact_digest": "sha256:REPLACE", "plan_digest": "sha256:REPLACE", "required_gates": ["certification"]},
  "artifact_kind": "source-candidate-v1",
  "policy_sha256": "REPLACE", "catalog_sha256": "REPLACE", "context_version": 0,
  "window_start": "2026-11-14T10:00:00+00:00", "window_end": "2026-11-14T10:01:00+00:00",
  "checks": {
    "TRACES-001": {"applicable": true, "scenario": "reviewed HTTP synthetic scenario", "expected": [], "justification": ""}
  }
}
```

This is a shape illustration, not a valid full fixture: include **every** check ID and actual matching values. `scenario` names the independently exercised/assessed scenario. `expected` supplies nonempty exact coverage sets, expected linked span IDs, or a single expected numeric value where the predicate requires it. Do not derive the oracle from the result under test. Other predicates use reviewed thresholds/allowed values and may have an empty expected list.

A not-applicable rule uses `source:null`, empty selector/parameters, and must match an assessor entry with `applicable:false`, a scenario and a nonempty evidence-backed justification. Core identity/structured-log/availability checks cannot be marked not applicable; an all-NA certificate is impossible. Applicability is trusted because of the separate authenticated assessor and reviewed policy, not because the system independently discovers service capabilities. Automated discovery/fixture execution is separate work.

Assessor and collector evidence must be fresh and collected between the end of the observation window and 120 seconds afterward. This accounts for query/fixture capture latency without changing the data window. The window must be completed, at most one hour long and within the past 24 hours. Scope generation and context must remain current. Captures expire; replays cannot renew timestamps.

## Run certification

Create an owner-only input file with `schema_version:2`, `release`, `artifact_kind`, `window_start`, `window_end`, `context_version`, `policy_sha256`, `fixture` (complete returned Evidence metadata), and `observations` (a list of `{"source":"profile-id","evidence":{...}}`). Source IDs must be unique. Missing sources yield INCOMPLETE rather than disappearing from coverage.

Local evaluation reads authenticated evidence and writes a new private report, without uploading it:

```sh
python -m sre_suite.verification run \
  --policy /absolute/private/certification-policy.json \
  --input /absolute/private/certification-input.json \
  --journal /absolute/private/certification-journal \
  --output /absolute/private/certification-report.json
```

Exit 0 means PASSED, 2 means FAILED/INCOMPLETE, 1 means configuration/runtime failure. Output is exclusive and never overwrites an existing file. The private journal stores immutable report bytes for retries. Policy/input files are bounded to 1 MiB/256 KiB, with shared JSON depth/node limits.

For durable verification and authenticated report publication, run a dedicated worker in the same configured Temporal namespace. It uses `SRE_CERTIFICATION_TASK_QUEUE` (default `sre-certification-v1`) and the verifier's private `SRE_EVIDENCE_API_*` credentials. The local-pilot launcher does not start this additional worker automatically.

```sh
python -m sre_suite.verification worker \
  --policy /absolute/private/certification-policy.json \
  --journal /absolute/private/certification-journal
python -m sre_suite.verification start \
  --policy /absolute/private/certification-policy.json \
  --input /absolute/private/certification-input.json --authorize-verification
python -m sre_suite.verification result WORKFLOW_ID
python -m sre_suite.verification cancel WORKFLOW_ID
```

Start is explicit and duplicate workflow IDs are rejected. The workflow does not start engineering, collect new telemetry or run deployment actions. Each activity invokes an isolated installed verifier process with a 300-second ceiling and minimal environment. The engine has a 120-second cooperative evaluation/read budget; the independent process alarm and supervisor cover blocking API calls. Three activity attempts and a 12-minute retry envelope fit a 15-minute workflow limit. Cancellation kills active verification; previously committed evidence is retained. Temporal history contains input references and compact report receipts, not raw telemetry.

## Verdicts and consumption

Every report contains exactly 120 unique results and the catalog/policy digests, exact release/window/context, evaluation/expiry times, reasons and evidence references. PASS requires a deterministic predicate success. An applicable mandatory FAIL yields FAILED when the rest of the report is complete. Missing/insufficient records, empty/partial responses, malformed/stale/wrong-binding evidence, unsupported fixture assessments and errors prevent certification. INCOMPLETE takes precedence over FAILED while retaining individual failures. NOT_APPLICABLE remains explicit and does not inflate the PASS count.

`CertificationEngine.gate(input, receipt)` reads the published report through the authenticated API, verifies its exact binding and 120-slot outcomes, and rechecks underlying evidence freshness/current generation/context before returning a typed `certification` gate. A receipt string or historical PASSED file alone is not a current gate. The legacy `certify` function remains an aggregation primitive for trusted typed results, not a verifier or approval service.

Without certification configuration, engineering `ready_for_review` means engineering/observation readiness. Version 0.17 can additionally require the current certificate through the [engineering integration](engineering-certification.md); standalone certification remains available. A passing certificate provides no production approval: the existing exact-release promotion policy still requires explicit authorized human approval, and this repository has no merge/deployment executor. Live source/schema/threshold acceptance, fixture runners, build/deployment provenance, cross-host journal recovery and authenticated assessor governance remain separate acceptance work.

See [ADR-019](adr/019-deterministic-certification.md), [engine](../sre_suite/verification/runtime.py), [predicates](../sre_suite/verification/predicates.py) and [tests](../tests/test_verification.py). Protocol references consulted: [OpenTelemetry trace API](https://opentelemetry.io/docs/specs/otel/trace/api/) and [Prometheus query values/histograms](https://prometheus.io/docs/prometheus/latest/querying/basics/).

Mapping predicates compare explicit source/target pairs (for example HTTP status to span status or log severity), with type-sensitive scalar values. Enum predicates also support numeric attributes. Span links compare both trace and span IDs. Timing permits asynchronous children to outlive parents and enforces causal starts with an explicitly reviewed clock-skew allowance.

For automatic child dispatch within an explicitly started engineering run, see [engineering certification](engineering-certification.md). Its private configuration, source compatibility checks and bounded independent fixture handoff are required before certification can gate review readiness.
