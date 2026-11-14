# Independent fixture runner and assessor

Version 0.18 implements a bounded fixture execution agent and an independent deterministic assessor for the 120-check certification workflow. They use separate API subjects, credentials, queues and journals. An explicitly started engineering run can dispatch them automatically during its frozen observation window. The assessor's expected telemetry values come from reviewed private policy, never from the telemetry being certified or a model-generated answer.

This is a real HTTPS scenario-controller adapter. It POSTs reviewed fixture commands to a single authorized non-production origin, records responses, and independently checks their assertions. The [packaged demo fixture controller](demo-fixture-controller.md) implements this protocol for one explicitly pinned demo source recipe. Other targets must implement their service-specific traffic/capability probes. Credentials alone cannot provide that target behavior or choose meaningful expectations. This package does not provision a controller, inject arbitrary code into a target, or provide universal gRPC/database/messaging traffic for unknown services.

## Private policy

Start with the [verification policy](certification.md) and create a private scaffold:

```sh
python -m sre_suite.fixtures template \
  --verification-policy /absolute/private/certification-policy.json \
  --output /absolute/private/fixture-policy.json
```

The scaffold is intentionally invalid until reviewed. Fill these exact fields:

| Field | Contract |
| --- | --- |
| `schema_version` | `2` |
| `verification_policy` | The complete reviewed 120-check policy, identical to the verifier's policy |
| `runner` | A fourth API subject, distinct from collector, assessor and verifier |
| `target` | One credential-free HTTPS origin, fixture-only authorization environment variable and optional loopback-only CA variable |
| `runner_queue`, `assessor_queue` | Separate Temporal queues; both distinct from the certification worker queue |
| `scenarios` | 1–40 named, bounded scenario POSTs in execution order |
| `checks` | Exactly 120 mappings to executed scenarios or capability probes, each with fixed expected values and applicability justification |

For example, one scenario entry could be:

```json
{
  "id": "http-error-journey",
  "path": "/fixtures/http-error-journey",
  "payload": {"requests": 3, "case": "controlled-error"},
  "assertions": {"/completed_requests": 3, "/expected_errors": 3}
}
```

The target is shaped as follows (replace the synthetic origin privately):

```json
{
  "base_url": "https://fixtures.example.invalid",
  "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
  "ca_env": null
}
```

Assertions use bounded JSON-pointer/scalar equality, not arbitrary code. Payloads are JSON objects of at most 8 KiB. Endpoints must be exact relative paths; URL interpolation, query strings, traversal and redirects are rejected. Authorization is supplied privately as the complete header value, for example `Bearer ...`. The runner does not receive Jira/GitHub/model credentials. The assessor receives evidence credentials only, including no target credential.

Each check mapping is `{ "scenario": "reviewed-scenario-id", "expected": [...], "justification": "..." }`. Applicability comes from the embedded verification policy. Even a not-applicable slot must map to a successfully executed capability probe and include a reviewed justification; core checks remain mandatory. A probe returning the unexpected capability state blocks assessment. Every declared scenario must serve at least one check. All 120 slots are emitted; checks cannot silently disappear. Coverage, expected numerical values and trace-link checks require nonempty independent expected values.

```sh
python -m sre_suite.fixtures validate --policy /absolute/private/fixture-policy.json
```

Validation is offline and prints the policy fingerprint and queue routing. Changing any reviewed scenario, oracle, endpoint or subject changes that fingerprint and requires a new reviewed engineering execution.

## Scenario-controller protocol

The runner sends one HTTPS POST for each configured scenario. The body contains:

- `schema_version: 2`, `scenario`, and a stable `idempotency_key`.
- `binding`: exact release (source-candidate artifact and engineering manifest plan), preview scope/generation, observation window, context version, verification policy and catalog fingerprints.
- `policy_sha256`: the fixture-policy fingerprint.
- `trace_ids`: prearranged full-trace IDs from the verification sources.
- `payload`: only the reviewed scenario parameters.

The controller must authorize this runner and scope, validate the candidate against its own trusted target metadata, execute the requested probe against that candidate within the window, and return:

```text
schema_version, binding, policy_sha256, scenario, idempotency_key,
executed_at (timezone-aware timestamp), result (JSON object)
```

The first five fields must exactly match the request. A controller must not merely echo unverified candidate metadata. The runner rejects mismatched identities, out-of-window/future timestamps and responses over 16 KiB. Transport is HTTP 200 JSON; expected application errors belong in the structured result and scenario assertions. HTTP failures, invalid JSON, TLS errors and redirects block/retry according to their classification. The generic transport also applies an absolute response-size bound before parsing.

The controller must durably deduplicate each key **and validate that repeated payloads are identical**, returning the original result/timestamp after a lost acknowledgement. The runner saves intent before POST and always retries with the same key. It cannot make a non-idempotent target exactly-once. A crash or lost response after an effect can require reconciliation; a missed window never triggers a fresh external effect. Do not expose arbitrary shell/SQL/URL inputs through this endpoint. Scenario implementations and authorization are target-specific trust boundaries.

Trace context must propagate through the actual workload using the configured identities and [W3C trace-context rules](https://www.w3.org/TR/trace-context/). Supplying a trace ID to the controller is not proof that instrumentation emitted it: the later Tempo/verification gates still check observed traces. A controller response is not a cryptographic deployed-image attestation. All current artifact bindings remain `source-candidate-v1`.

## Separate worker processes

Install the orchestration extra. Use the same Temporal service/namespace for all roles, separate physical owner-only directories outside Git, and the same reviewed fixture policy on both workers.

In the **runner** process environment, configure its exact-scope evidence credential plus `SRE_FIXTURE_TARGET_AUTH`. For local HTTPS acceptance only, `ca_env` may name `SRE_FIXTURE_TARGET_CA_FILE`; the CA override is restricted to localhost/127.0.0.1. Never disable TLS verification.

```sh
python -m sre_suite.fixtures worker --role runner \
  --policy /absolute/private/fixture-policy.json \
  --journal /absolute/private/fixture-runner-journal
```

In a separate **assessor** process, configure the assessor's evidence credential. It needs read access to the runner proof and context plus upload access for its own assessment, but no target credential.

```sh
python -m sre_suite.fixtures worker --role assessor \
  --policy /absolute/private/fixture-policy.json \
  --journal /absolute/private/fixture-assessor-journal
```

Runner execution, runner publication and assessment occur in separate supervised processes with a 300-second limit, bounded output and role-filtered environments. Temporal owns the start/end timers, activity retries and cancellation. The runner acts only during the window (at most five minutes); after window end it publishes immutable execution evidence. The assessor rereads the actual runner bytes, checks producer, scope, release, all scenario responses and fixed assertions, and uploads the full assessment. Publication/assessment must complete within 120 seconds after the window. Evidence expires no later than 15 minutes after window end. Short windows must leave enough time for all sequential probes and worker scheduling; there is no automatic expansion of the planned window.

## Automatic engineering handoff

Add the exact dispatch returned by validation as `fixture` in the existing private engineering-certification configuration:

```json
{
  "policy_sha256": "REPLACE_WITH_REVIEWED_FIXTURE_POLICY_SHA256",
  "runner_queue": "sre-fixture-runner-v1",
  "task_queue": "sre-fixture-assessor-v1"
}
```

The surrounding certification `policy`, verifier `task_queue` and `fixture_wait_seconds` fields remain unchanged. `check-config`/preflight validate this routing; they do not prove live fixture-worker/controller readiness. Start all required worker processes before explicitly starting the engineering run. The local-pilot lifecycle does not automatically launch these workers or provision their credentials.

The parent freezes dispatch into the engineering manifest and certification plan. It starts `FixtureAssessment` before telemetry collection, awaits collection and assessment, then supplies the assessor's evidence directly to certification. With automatic dispatch configured, manual fixture signals cannot replace or bypass the assessor. Failed scenarios, incomplete assessment, missed windows or stale scope block certification and review readiness. The old independently supplied fixture path remains available when automatic dispatch is absent.

The assessment references the runner proof. Certification reads that proof and includes it in the immutable certificate's evidence/expiry boundary. Gate consumption rechecks it, the assessor evidence and telemetry against current preview/context state. A cached PASSED certificate is rejected if fresh evaluation can no longer validate its evidence. A provisional draft published before collection may remain; no merge, deployment or production approval follows from successful fixtures.

For a separately reviewed standalone run, a private request has `{ "plan": <exported certification plan>, "dispatch": <validated dispatch> }`:

```sh
python -m sre_suite.fixtures start \
  --policy /absolute/private/fixture-policy.json \
  --request /absolute/private/fixture-request.json --authorize-fixtures
```

Do not also start a standalone run for an automatic engineering child: the parent owns that workflow identity. Duplicate identities are rejected. Use the existing Temporal result/cancellation tooling to inspect or stop a standalone execution. The authorization flag permits the reviewed fixture effects and evidence writes; it grants no production or deployment authority.

## Validation limits

Tests use real authenticated scoped evidence, synthetic controller responses, loopback HTTPS and a real local Temporal engine. They cover all 120 assessments through the full engineering/certification path, separate identities, response binding, lost acknowledgements, deduplication keys, failed probes, not-applicable decisions, expiry/generation, replay, restart and cancellation. No live private controller, service backend or credentials have been exercised. Target-side deduplication durability, truthful candidate metadata, meaningful service-specific probes and cross-host journal recovery need private acceptance.

## Managed local demo option

Version 0.20 supports [managed target startup and durable export](managed-demo-pilot.md) within an explicitly started pilot. The optional reviewed dispatch preserves exact artifact, plan, preview ID and generation checks and orders export/assessment before telemetry collection. Separate worker authorization and private role credentials are required.
