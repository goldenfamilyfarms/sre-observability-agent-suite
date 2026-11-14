# Demo instrumentation and reviewed certification policy

Version 0.23 supplies the measurements missing from the local demo's first full certification run. The real backend acceptance target is **76 PASS and 44 NOT_APPLICABLE**, across all 120 slots. The 44 exclusions still require the pinned capability probe; no applicable check was made optional. This is certification of the reviewed **source candidate and synthetic journey**, not deployed image provenance or production readiness.

## What is measured

| Concern | Evidence and interpretation |
| --- | --- |
| Exception stack | Actual caught traceback function/line locations, bounded to eight frames; no filesystem paths, source text, locals, or request inputs. Logs and OTLP exception events carry the same safe information. |
| Process | Serving process cumulative CPU seconds, current resident bytes, monotonic lifetime and open file descriptors. Linux uses `/proc`; macOS uses a bounded `ps` read for current RSS. Unsupported or denied measurements are omitted, never fabricated as zero. |
| Requests/errors | Committed six-request journey counters divided by elapsed observation-window seconds. This is the finite journey's average rate, not a five-minute steady-state `rate()`. |
| Latency | Seconds-based histogram, independently reviewed 1 ms journey objective, median histogram estimate (0.5 ms with 0.5 ms tolerance), and the fraction within the objective. Slow runs fail the reviewed thresholds. |
| Scraping | Actual Prometheus `up`, scrape duration, scraped samples / enforced 500 sample limit, and sample timestamps. Freshness compares each evaluation timestamp to the original sample time (three-second allowance). |
| Export | Durable per-batch attempts, failures and acknowledged timestamp; delivered flags are committed with acknowledgement timestamps after current-generation guards. Failed/partial acknowledgements remain pending. Recovery retries identical payloads. |
| Log ingestion | Maximum event-to-successful-Loki-acknowledgement delay for the batch (10 seconds). This bounds acceptance by the ingest endpoint, not query visibility, replication or long-term disk durability. Separate real Loki retrieval and coverage checks prove the journey is subsequently queryable. |
| Loss/queue | Actual pending durable batches and audited differences between committed event counts and immutable outbox payload item counts. This measures local pipeline loss, not every possible downstream discard after acknowledgement; separate trace/log coverage checks test the backend result. |
| Retention | Read-only, authenticated gateway probe checks Loki readiness, generated stack configuration and Loki's **effective `/config`**. It emits 86400 seconds only for enabled compactor retention, filesystem delete store and a 24-hour retention period. It does not claim a short pilot observed expiration/deletion. |
| SLI calculations | Exactly one server error in six requests yields availability 5/6 and a burn rate of (1/6)/0.01 against the reviewed 99% availability objective. A passing calculation check does not mean the demo meets that availability objective. |

Prometheus evaluates the shipped recording rules every second. Every derived service series retains service, environment, revision, preview ID, generation, artifact digest and plan digest. Backend retention measurements carry the five preview dimensions and join the **actually scraped** target identity before certification. `up` and scrape timings join the target's job/instance, not an arbitrary service label. Empty/missing series still block the verifier. Metric label dimensions `case`, `le`, `signal`, `unit`, `check`, `series` and `__name__` cannot be used as binding labels.

## Explicit policy preparation

Start from a fresh managed-demo policy with exactly one base source for each backend, the current exact demo recipe, distinct evidence principals, and the standard local-stack Prometheus label mapping. Use the existing private stack setup; add this option when generating the consistent worker bundle:

```sh
python -m sre_suite.local_telemetry prepare-certification \
  --directory /private/path/telemetry \
  --managed-policy /private/path/managed-demo.json \
  --output /private/path/instrumented-certification \
  --window-seconds 30 \
  --demo-instrumentation
```

The option explicitly selects the shipped demo assertions, thresholds, six sources and independent journey expectations. Review the resulting policies and fingerprints before use. Without the option, preparation continues to preserve the supplied assertions and expectations. It never starts a workflow, writes Jira, merges or deploys. Workflows still start explicitly, with privately provided credentials.

## Migration and trust limits

Catalog **1.1.0** changes TRACES-039, LOGS-034 and LOGS-036 to `metric_range.v1` with a required Prometheus source. These concerns need infrastructure metrics; trace/log application fields cannot establish delivery or backend retention. Other check source/backend requirements remain unchanged. Sample freshness now compares each sample with its own evaluation time, rather than the final window time.

Old catalog fingerprints are rejected. Regenerate private verification/fixture/managed/engineering policies, review the new candidate artifact and plan digests, and start a fresh run. Do not reuse old certificates, execution plans or controller state. New controller databases add a durable delivery table without rewriting saved payloads; old acknowledged rows without measured timestamps cannot supply ingestion-latency evidence.

The local stack now enables Loki compactor retention and mounts reviewed Prometheus recording rules plus a separate gateway read credential. Existing generated stack configurations intentionally fail the exact-config check; prepare a fresh private stack directory. Retained data is not silently migrated or deleted. Schema identity, exact artifact/plan, preview generation, context version, authenticated producer, independent runner/assessor/verifier and report recovery checks remain enforced.

The local gateway trusts the host that runs the pinned backend containers. This is not a remote backend attestation scheme or a multi-tenant security boundary. See [Loki retention documentation](https://grafana.com/docs/loki/latest/operations/storage/retention/) and [Prometheus recording rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) for the underlying backend mechanisms.
