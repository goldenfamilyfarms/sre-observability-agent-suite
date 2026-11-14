# Managed demo startup and durable export

Version 0.20 adds an opt-in lifecycle worker to the [pinned demo](demo-fixture-controller.md). After an explicit engineering workflow start, its fixture child starts the exact reviewed target, verifies authenticated HTTPS readiness, runs the three reviewed scenarios, exports both committed telemetry batches, and obtains the independent assessment. Only then does the parent start telemetry collection and certification. A failed startup, export or assessment blocks collection and review readiness.

This is a bounded local source-candidate pilot. It executes the packaged demo component only; it does not execute arbitrary candidate code, provision GCP/GKE, install a chart, merge a PR, promote an environment or deploy a service. The exact recipe equality and all artifact, plan, context, preview ID and generation checks remain required. [Private verification mappings and independent expected values](demo-fixture-controller.md#prepare-offline) are still necessary. An ingestion acknowledgement alone cannot certify a service.

## Private configuration

First prepare the fixture policy and exact source recipe using the [controller guide](demo-fixture-controller.md). Create a mode-0600 JSON file in an owner-only physical directory outside Git with these fields (the table describes values, not literal placeholder objects):

| Field | Required value |
| --- | --- |
| `schema_version` | `2` |
| `fixture_policy` | Complete reviewed fixture policy object, including verification policy and all 120 scenario mappings |
| `recipe` | Complete output of `demo_fixture recipe`; exact repository/base revision and packaged `demo_service.py` edit |
| `task_queue` | Dedicated lifecycle queue, separate from runner, assessor, collector and verifier queues |
| `startup_seconds` | Integer 5–60; reserve this much lead time before the fixed observation window |
| `settle_seconds` | Integer 1–60; reserve this much time between successful export and window end |
| `certificate`, `private_key` | Absolute private paths to the target certificate and key, valid for localhost/127.0.0.1 |
| `export` | The two-origin object from [the export example](../examples/demo-fixture-export.json), with privately reviewed HTTPS ingestion endpoints |

The fixture target must be `https://localhost:PORT` or `https://127.0.0.1:PORT`, with an explicit unprivileged port and `SRE_FIXTURE_TARGET_CA_FILE` configured. The listener always binds 127.0.0.1. Export custom CA overrides are loopback-only; remote origins require normal trusted TLS. Endpoint paths remain fixed (`/v1/traces` and `/loki/api/v1/push`). Configuration validation makes no network calls.

```sh
python -m sre_suite.demo_fixture.lifecycle validate \
  --config /absolute/private/managed-demo.json
```

Copy the returned `demo` object into the private engineering certification configuration alongside its existing `fixture` dispatch. It contains only the full private policy's SHA-256, queue and timing budgets. Set the engineering telemetry `delay_seconds` to at least `startup_seconds`, and `window_seconds` greater than `settle_seconds`. Allow practical startup, fixture, export retry and backend ingestion time within those budgets. Both configurations and the resulting dispatch are frozen into the engineering plan. Changing a policy/endpoint/recipe/path requires revalidation and a new reviewed run; it cannot silently alter an in-flight plan.

Managed runs pin their per-run target from the authorized immutable plan, so the manual `demo_fixture pin`/`serve`/`export` sequence is unnecessary for those runs. The legacy manual route remains available when `demo` is absent. Never run the two controllers on the same port/state.

## Start the dedicated worker explicitly

Install the orchestration extra and create an owner-only persistent state directory. Set these values privately in the lifecycle worker environment:

- Exact-scope read-only evidence context grant: `SRE_EVIDENCE_API_URL`, `SRE_EVIDENCE_API_TOKEN`, and loopback `SRE_EVIDENCE_CA_FILE` when needed.
- Three distinct full Bearer headers, each with at least 32 token characters: `SRE_DEMO_FIXTURE_AUTH`, `SRE_DEMO_METRICS_AUTH`, `SRE_DEMO_LIFECYCLE_AUTH`.
- Signal-specific ingest headers: `SRE_DEMO_TRACES_AUTH`, `SRE_DEMO_LOGS_AUTH`; optional tenant variables and loopback export CA as in the controller guide.
- The existing `SRE_TEMPORAL_*` connection settings for the same pilot namespace. The queue comes from the reviewed policy.

```sh
python -m sre_suite.demo_fixture.lifecycle worker \
  --config /absolute/private/managed-demo.json \
  --root /absolute/private/demo-state \
  --authorize-targets --authorize-export
```

Both authorization flags are required before connecting to Temporal. Starting this worker does not start a workflow or immediately bind the target port. It authorizes bounded targets and exports for matching future dispatches. Keep runner, assessor, collector and verifier workers running separately with their existing independent policies and credentials. The runner receives the fixture token as `SRE_FIXTURE_TARGET_AUTH` plus the target CA; the scraper receives only the metrics token. Neither gets the lifecycle token or ingest credentials.

For the [local pilot supervisor](local-pilot.md), provide a separate mode-0600 JSON object whose keys are only `SRE_EVIDENCE_API_TOKEN`, the five required `SRE_DEMO_*_AUTH` keys above, and any required `SRE_DEMO_TRACES_TENANT`, `SRE_DEMO_LOGS_TENANT`, `SRE_DEMO_EXPORT_CA_FILE`. All values are strings. Register its independent exact-scope context credential in the private API credential configuration; initialization does not manufacture new role grants.

```sh
python -m sre_suite.local_pilot up \
  --directory /absolute/private/sre-pilot/stack \
  --worker-env /absolute/private/worker.json \
  --demo-config /absolute/private/managed-demo.json \
  --demo-credentials /absolute/private/demo-credentials.json \
  --authorize-demo
```

The supervisor supplies its own local evidence URL/CA and Temporal namespace, starts the lifecycle worker after API readiness, waits for its activity poller, and stops owned processes on shutdown. Demo state persists under `stack/demo-state`. This option does not start the other independent fixture/telemetry/verifier workers. The redacted ready record distinguishes `demo_worker_started` from `execution_started: false`.

After preflight and review, use the existing explicit engineering start command. A standalone managed `python -m sre_suite.fixtures start` additionally requires `--authorize-demo` alongside `--authorize-fixtures`. No startup, validation or handoff command starts an execution implicitly.

## Ordering, recovery and failure

The lifecycle worker runs target serving, readiness and export in separate bounded subprocesses. The serving child receives fixture/metrics/readiness credentials; the export child receives ingest credentials. Neither receives Jira, GitHub or model authority. Commands are fixed installed modules, not user-provided shell strings.

The target starts no earlier than its startup allowance. Readiness returns exact workflow, binding and private-policy hashes over authenticated TLS. Fixture execution remains forbidden before window start. A per-run filesystem lock owns the listener; another run cannot adopt it. The child periodically checks current context, has a parent-loss watchdog and an absolute lifetime bound. Cancellation waits for the owned process group to terminate. It never kills a PID discovered from an unrelated file or occupied port.

After all three scenarios commit, export requires exactly the complete logs and traces outbox. Temporal retries transient failures, while the private SQLite outbox and completion journal survive worker restart. Successful batches are not resent after a recorded acknowledgement. A lost backend acknowledgement can cause the same immutable batch to be delivered again: downstream ingestion is **at least once**, not exactly once. Partial OTLP rejection is a failure.

New delivery must finish strictly before `window_end - settle_seconds`, with scope/context checked before and after each request. The durable completion receipt binds the exact run, policy, outbox digest and completion time. A lost activity acknowledgement returns that saved receipt without re-export, but still checks current generation and expiry. If completion was not durably recorded before the cutoff, a late retry blocks. No observation window is extended.

The fixture workflow waits for window end, publishes runner proof, invokes the independent assessor, and stops its target before returning. The engineering parent then queries the fixed window through its existing telemetry children and performs all 120 checks. Configure Prometheus scraping during the window, including independent health evidence. Export settlement is a bounded allowance, not proof of query visibility; missing or delayed telemetry still fails certification.

Use one lifecycle worker host, a dedicated queue and persistent private state. Cross-host failover, concurrent distinct targets sharing one port, host disaster recovery and a production service supervisor are not implemented. Local test sinks prove protocol, replay and recovery behavior; live Prometheus/Loki/Tempo acceptance remains private operator work.
