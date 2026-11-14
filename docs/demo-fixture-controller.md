# Demo fixture controller and reviewed scenarios

Version 0.19 adds a concrete target for the [independent fixture runner and assessor](fixture-agents.md). It is a completed, bounded Python demo packaged in `sre_suite.demo_fixture`. It preserves the separate pilot repository's deliberately missing log-correlation implementation and immutable acceptance tests.

The demo exercises six request-handler cases: health, an unknown route, an unsupported method, a controlled exception, Unicode, and a synthetic log-injection/redaction canary. A second probe tests 13 invalid trace contexts. A capability probe runs the pinned service and reports its fixed capability manifest. Inputs, counts, paths and assertions are code-reviewed constants; callers cannot provide URLs, shell commands, SQL, plugins or workload counts. Handler calls are **in-process**, not outbound HTTP load tests or multi-service traffic. The HTTPS fixture request has a server span; individual handler calls have internal spans. Only the allowlisted journey creates telemetry.

The [complete 120-slot mapping](demo-fixture-checks.md) leaves **76 checks applicable** and marks **44 checks not applicable** for genuinely absent features (gRPC, database, cache, messaging, outbound HTTP, async propagation, retry/timeout/cancellation and JVM). The assessor verifies the capability-manifest fingerprint from a live probe before accepting those exclusions. This mapping is specific to the pinned Python demo. It cannot be reused as a general service exemption list.

A completed fixture assessment is not a passing certification. Scrape health, process/exporter health, retention, SLO calculations and all other applicable concerns still require independently collected, correctly mapped backend evidence. The demo does not invent those observations, install scrape targets/recording rules, configure retention, or manufacture 120 passing records. The private verification policy must select the appropriate actual fields and queries; credentials alone do not supply that policy.

## Prepare offline

Use owner-only physical directories outside Git. All commands below create new private files exclusively; they never overwrite existing inputs or start a workflow. Replace paths and identifiers privately. Review the verification policy as described in the [certification guide](certification.md).

```sh
python -m sre_suite.demo_fixture matrix --output /absolute/private/check-mapping.json
python -m sre_suite.demo_fixture expectations-template --output /absolute/private/expectations.json
```

The expectation template contains ten null entries and is **intentionally invalid**. Fill every entry with reviewed, nonempty lists for the following independently chosen predicates:

| Checks | Oracle considerations |
| --- | --- |
| TRACES-036 | The demo samples its one trace fully (`demo.sampled` is `true`); align the selector and path with that policy |
| TRACES-040, LOGS-014, LOGS-035, LOGS-039 | The six distinct `case`/`event_id` values are `health`, `not-found`, `method`, `error`, `unicode`, `redaction`; select journey records, excluding the enclosing fixture server span |
| PROMQL-037 | The independently specified set of series/labels required by the reviewed PromQL expression |
| PROMQL-004, PROMQL-038, PROMQL-039, PROMQL-040 | Independent numerical expectations and tolerances for the exact quantile/SLI/burn expression and fixture window; never copy a query result into the oracle |

The controller measures actual handler durations. It supplies no guaranteed latency percentile or arbitrary SLO target. If an independent fixed oracle cannot support the selected query/window, revise the experiment and its private policy; do not weaken the gate to pass.

The verification policy must contain 3–20 sources covering Prometheus, Loki and exactly one full-trace Tempo source, a prearranged nonzero trace ID, and limits of at least seven records. It must preserve the mapping's applicability. Scope and artifact/plan label names come from those profiles, so customized mappings are honored. Multiple profiles for a backend must have identical identity mappings. Reserved metric dimensions (`case`, `le`, `__name__`) cannot also carry scope. Each new run should use a fresh trace ID and observation window.

```sh
python -m sre_suite.demo_fixture prepare \
  --verification-policy /absolute/private/verification-policy.json \
  --expectations /absolute/private/expectations.json \
  --target-origin https://localhost:9443 --loopback-ca \
  --runner demo-fixture-runner --output /absolute/private/fixture-policy.json
```

Supply the resulting dispatch to the existing engineering certification configuration. Runner and assessor setup remains in the [fixture agent guide](fixture-agents.md). They use separate credentials and queues.

## Pin the actual target independently

The controller never trusts request-supplied artifact metadata. Its private target configuration fixes the exact release, plan, preview generation, context version, window and policies before accepting requests. The artifact must be the SHA-256 of this exact source-candidate recipe:

```sh
python -m sre_suite.demo_fixture recipe \
  --repository YOUR_OWNER/YOUR_DEMO_REPOSITORY \
  --revision FULL_40_CHARACTER_BASE_REVISION \
  --output /absolute/private/demo-recipe.json
```

The recipe uses `source-candidate-v1`, the named repository/base revision, and one edit: `demo_service.py` containing the exact packaged `service.py` source bytes. The recipe command does not write to that repository. The authorized candidate must contain precisely this edit; arbitrary worker code, extra edits, another revision or another artifact are rejected. The controller runs the packaged implementation, never imports a candidate checkout, and is not a generic app runner. Installed controller/instrumentation code remains part of the trusted harness. This proves source-recipe equality for the exercised demo component, not a deployed OCI image, repository checkout attestation, or behavior of other files in that repository.

After separately reviewing the exported certification plan, form the existing fixture request `{ "plan": <exported plan>, "dispatch": <fixture dispatch> }` and pin it:

```sh
python -m sre_suite.demo_fixture pin \
  --policy /absolute/private/fixture-policy.json \
  --request /absolute/private/fixture-request.json \
  --recipe /absolute/private/demo-recipe.json \
  --output /absolute/private/target.json
python -m sre_suite.demo_fixture validate --target /absolute/private/target.json
```

The pin command is offline; it cannot attest a live preview or authorize deployment. The independently authenticated evidence context API is checked again on every execution, cached retry, scrape and export. A different generation, plan, policy or window requires a separate target and state directory. For this manual route, an automatically generated engineering plan must be pinned and the controller made ready before its observation window. Version 0.20 adds an opt-in [managed target startup and durable export path](managed-demo-pilot.md); cloud provisioning remains unimplemented. Allow enough dispatch delay for that explicit operator handoff, or use the existing standalone fixture start command with a reviewed future window.

## Explicit HTTPS startup

Provide a certificate valid for `localhost`/`127.0.0.1`, its private key and an empty owner-only state directory outside Git. The controller binds **127.0.0.1 only**. This local pilot is not a public ingress or production server.

Set privately in the controller process:

- `SRE_DEMO_FIXTURE_AUTH`: complete `Bearer ` header with at least 32 token characters.
- `SRE_DEMO_METRICS_AUTH`: a different token with the same format, for the scrape process only.
- `SRE_EVIDENCE_API_URL`, `SRE_EVIDENCE_API_TOKEN`, and optional loopback `SRE_EVIDENCE_CA_FILE`: an exact-scope read-only context grant; no upload, Jira, model or GitHub authority is needed.

Set `SRE_FIXTURE_TARGET_AUTH` to the fixture token in the **runner** environment, and `SRE_FIXTURE_TARGET_CA_FILE` to the trusted local certificate/CA. The assessor receives neither target credential. Do not put tokens in the public policy, shell history or repository.

```sh
python -m sre_suite.demo_fixture serve \
  --target /absolute/private/target.json --state /absolute/private/controller-state \
  --cert /absolute/private/server.crt --key /absolute/private/server.key \
  --port 9443 --authorize-fixtures
```

Only exact `POST /fixtures/capabilities`, `/fixtures/http-journey`, `/fixtures/invalid-context` commands and authenticated `GET /metrics` are accepted. The server rejects altered binding fields, payloads, trace IDs, idempotency keys, policy fingerprints, unknown endpoints, duplicate JSON, ambiguous framing and oversized bodies. TLS handshakes and request reads have five-second deadlines. Raw requests and credentials are not logged.

## Telemetry export and collection

The controller atomically commits the response, six event measurements and two immutable outbox batches in SQLite. Metrics expose `sre_demo_requests_total`, `sre_demo_request_duration_seconds_{bucket,sum,count}` and `sre_demo_outbox_pending`, with exact candidate/scope labels and six bounded `case` labels. Counts and durations come from actual handler calls. Trace IDs, secrets and raw paths never become metric labels. A successful scrape is not the same as Prometheus `up`, scrape health or a process metric; configure those independently.

The outbox contains OTLP/HTTP JSON traces and native Loki JSON streams. Trace and log IDs correlate; raw invalid context and the synthetic canary are omitted. Use [OTLP/HTTP JSON](https://opentelemetry.io/docs/specs/otlp/) ingestion for Tempo through your collector, and [Loki push](https://grafana.com/docs/loki/latest/reference/loki-http-api/) for logs. The OTLP origin must accept `/v1/traces`; the Loki origin must accept `/loki/api/v1/push`. An HTTP export acknowledgement is not proof of later query visibility.

Copy [the synthetic export configuration](../examples/demo-fixture-export.json) into a private directory, replace its two origins and set the referenced signal-specific authorization variables privately. Optional tenant variables are `SRE_DEMO_TRACES_TENANT` and `SRE_DEMO_LOGS_TENANT`. For loopback TLS tests only, `ca_env` may be `SRE_DEMO_EXPORT_CA_FILE`. No arbitrary path or non-HTTPS transport is supported.

After the journey commits, explicitly deliver its outbox **before the scheduled telemetry read**:

```sh
python -m sre_suite.demo_fixture export \
  --target /absolute/private/target.json --state /absolute/private/controller-state \
  --export-config /absolute/private/export.json --authorize-export
```

This command processes at most two pending batches and exits. It does not run in the server background, create schedules, or start workflows. Re-run after a transient failure; already acknowledged batches are skipped. Delivery requires current context and is bounded by the pinned window plus the existing 120-second collection grace. Arrange backend ingestion and scrape time within the observation window. If collection wins the race or a deadline is missed, certification must fail and a new reviewed run is required; no window is extended silently.

Only HTTP 200 is accepted for OTLP and HTTP 204 for Loki; other success-like codes do not acknowledge delivery. OTLP partial rejections or warning messages leave the batch pending. After a lost acknowledgement, delivery is **at least once**: the same stored bytes/IDs/timestamps may be sent again. The fixture's local durable effects and metric counts do not repeat, but downstream duplicate handling must be accepted privately. Do not claim exactly-once telemetry ingestion.

## Durability and validation boundaries

SQLite uses immediate write transactions and full synchronization. Pure in-process handler computation, responses, measurements and outbox insertion share one commit. An exception or crash before commit rolls them all back; a lost response after commit returns the original result on retry, including after restart and concurrent requests. New effects stop at window end; cached response recovery remains available within grace only with current scope. State identity prevents reopening a journal for another plan/generation. Persist this private directory on one host; cross-host ownership/failover is not provided.

Tests cover the real controller, authenticated evidence API, all 120 assessment slots, missing-telemetry rejection, emitted trace/log adapter round trips, correlation failure, durable retries, rollback, tampering, expired/replaced generations, private CLI preparation, real loopback TLS, credential separation and export acknowledgements. They use synthetic credentials and local sinks, not a live Grafana stack. No service is certified by these test counts. Live scrape/ingestion/query configuration, private policy/oracle review and independent backend health evidence remain operator acceptance work. No merge, deployment or production approval is performed or added.
