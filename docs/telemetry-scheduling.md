# Full Tempo traces and durable telemetry collection

Version 0.14 adds full Tempo V2 trace retrieval and a dedicated, explicitly started Temporal telemetry workflow. It does not start the Jira engineering workflow, perform deployment actions or create an endless background schedule. All credentials and infrastructure values remain private configuration.

## Full trace collection

In a privately reviewed Tempo profile, set `allow_full_traces` to `true`. It defaults to false in existing policies. Use the [synthetic full-trace request](../examples/telemetry-trace-request.json) as a starting point. In the query request add `trace_id`, a nonzero 32-character lowercase hexadecimal identity. Use a recent, whole-second observation window and a `limit` of at most 500 spans. The existing `python -m sre_suite.telemetry` command then fetches `GET /api/v2/traces/{id}` with explicit start/end bounds and `span_pruning=false`; it does not send a TraceQL filter that would remove nonmatching spans. Search and Prometheus/Loki requests continue to work unchanged. No V1 fallback is attempted.

Review the full-trace permission separately: a distributed trace may contain other services' spans. The collector requires at least one resource with all five exact scope attributes and a span overlapping the requested window. Every returned span must carry the requested trace identity. It marks which spans match the requested scope, retaining cross-service context without claiming that context belongs to the target preview. Generation must remain a string resource attribute, consistent with the query policy mappings. These checks establish reported attribution, not instrumentation truth or artifact/plan identity.

The bounded OTLP payload is preserved in private evidence, including resources, instrumentation scopes, attributes, events, links, status and extension fields. Typed span summaries accompany it. Current `resourceSpans` / `scopeSpans` and legacy `batches` / `instrumentationLibrarySpans` are supported, with canonical hex and protobuf base64 identifiers normalized for validation. Ambiguous representations, wrong IDs, duplicate spans/attributes, cycles, invalid values and exceeded budgets fail closed.

Backend `PARTIAL`, pruning, dropped-data counters, missing parents and abnormal root counts produce a partial observation. Unknown completeness/pruning enums are rejected. An omitted completeness enum is protobuf's `COMPLETE` zero value. `observed` still does not prove all spans were emitted, ingested or retained. Tempo's time bounds restrict searched blocks; sampling, retention or blocks outside the window can omit data even when the server reports completion. The 120-check catalog remains a design inventory; no collection issues a certificate.

## Private schedule preparation

Install the optional runtime with `python -m pip install '.[orchestration]'`. Create an owner-only directory outside Git for the reviewed policy, schedule and durable journal; files must be mode `0600`, directories `0700`, without symlink aliases. Use the [example schedule](../examples/telemetry-schedule.json) as a structure, updating all synthetic scope/time values. Its request can be any approved Prometheus/Loki/Tempo query, or a full-trace request with an authorized `trace_id`. One schedule collects one profile. Tempo search results are not automatically expanded into trace fetches.

Compute the fingerprint after reviewing the actual private policy:

```sh
python -m sre_suite.telemetry.scheduling fingerprint \
  --policy /absolute/private/telemetry-policy.json
```

Copy `policy_sha256` into the schedule. The fingerprint binds scope, endpoints, query templates, mappings, credential variable names and full-trace permission; credential values are never included. A changed policy requires a newly reviewed schedule. Keep the actual credentials/tenant values under operator control; the fingerprint does not authenticate a remote tenant or an operator's identity.

The schedule has a unique `schedule_id`, request, `first_run_at`, `interval_seconds`, `occurrences` and `max_lateness_seconds`. The first collection must be current or at most one hour in the future, and follow the request window by 0–300 seconds. It can repeat 1–24 times, every 1–3600 seconds. Each subsequent window advances by the fixed interval. Lateness is bounded to 1–300 seconds. A missed window stops the workflow rather than generating a catch-up burst; collections never overlap within one workflow. Preview and credential expiry can stop a schedule before its planned end.

Offline validation makes no API calls and starts nothing:

```sh
python -m sre_suite.telemetry.scheduling validate \
  --policy /absolute/private/telemetry-policy.json \
  --schedule /absolute/private/telemetry-schedule.json
```

## Dedicated worker and explicit start

Configure the [existing telemetry credentials](telemetry-adapters.md#private-setup), plus `SRE_TEMPORAL_ADDRESS`, `SRE_TEMPORAL_NAMESPACE`, `SRE_TEMPORAL_TLS` and, if needed, `SRE_TEMPORAL_API_KEY`. Non-loopback Temporal addresses require TLS. Set a dedicated `SRE_TELEMETRY_TASK_QUEUE` (default `sre-telemetry-v1`); this queue is separate from the engineering worker queue. The current local-pilot lifecycle does not launch this additional worker automatically.

Create the private journal directory before starting the worker. It must be durable, shared by replacement workers with tested POSIX locks, and dedicated to this namespace and trust boundary. The worker consumes the policy directly; do not put telemetry credentials in model-worker configuration.

```sh
python -m sre_suite.telemetry.scheduling worker \
  --policy /absolute/private/telemetry-policy.json \
  --journal /absolute/private/telemetry-journal
```

The following command checks the current evidence API scope and explicitly authorizes the finite collection plan **including evidence uploads**. Merely validating a schedule or starting a worker does not invoke it.

```sh
python -m sre_suite.telemetry.scheduling start \
  --policy /absolute/private/telemetry-policy.json \
  --schedule /absolute/private/telemetry-schedule.json \
  --authorize-collection
```

The deterministic workflow ID binds `schedule_id` and the complete preview scope. Active or completed duplicate IDs are rejected within Temporal's retention. Changing intervals under the same ID is not a new authorization; choose a new reviewed schedule name for a new run. The worker also rejects an occurrence journal created for different settings. Namespace permissions and operator access govern who can submit schedules; a local policy fingerprint is not an authenticated approval service.

Use `python -m sre_suite.telemetry.scheduling status WORKFLOW_ID`, `result WORKFLOW_ID` or `cancel WORKFLOW_ID`. Cancellation during a timer prevents the next collection. Activity cancellation heartbeats terminate the isolated child process. Cancellation cannot undo an already accepted evidence write, and server-side requests may finish after the client disconnects; generation checks still govern evidence validity.

## Recovery and bounded execution

Temporal owns timers, retries and progression. The worker runs at most two activities concurrently. Each occurrence runs in a separate installed Python child with a 60-second total deadline, bounded output and process-group cleanup. The child also sets its own POSIX alarm so the ceiling survives loss of the supervisor. It receives only evidence and configured telemetry credentials plus minimal process/TLS environment; Temporal, Jira, GitHub, model credentials and Python injection settings are excluded. The activity has a 90-second attempt timeout, five-second heartbeat timeout and 180-second retry budget, with at most three attempts. Transport errors, HTTP 429 and 5xx may retry; invalid data/scope, authorization failures and process budget exhaustion block the workflow. Overall execution is capped at 26 hours by the start command.

The child saves the normalized observation atomically **before** uploading it. A lost upload response or activity acknowledgement reuses identical content, ID, observation time and expiry instead of querying the source again. Every reuse checks current scope, lateness, evidence age and the saved context version. Upload is idempotent and followed by byte verification and another generation/context check. Context changes can conservatively block reuse. A crash before capture persistence can repeat a read; no exactly-once external query guarantee is claimed. Expired evidence is not refreshed with new timestamps. Completed evidence remains historical; later collections do not extend its 15-minute validity.

Workflow history contains the scoped schedule, policy hash, status and compact evidence references. Query/trace bodies and credentials remain in the private journal/evidence service. `collected` means the requested collection operations finished, including any partial/empty observations; it is not a service-health, gate or certification verdict. Backend assertion failures require future deterministic verification logic. The engineering workflow is not implicitly wired to this scheduler.

## Validation and open acceptance

Default tests exercise complete/partial/pruned traces, legacy encodings, exact scope/window attribution, private child entrypoints and retry-safe journal/evidence behavior. Loopback HTTPS tests exercise actual V2 paths and headers. Real disposable Temporal tests cover timers, replay, duplicate starts, lost responses, restart, bounded retries, stale generations, active/timer cancellation and process deadlines. The engine data-path tests use synthetic sources and the real authenticated SQLite evidence API; process supervision tests use disposable blocking children. Live Tempo, production Temporal hosting, cross-host storage failure recovery and organization-specific tenant/schema acceptance remain unverified.

Protocol references: [Tempo V2 API](https://grafana.com/docs/tempo/latest/api_docs/#query-v2), [Tempo response/status definitions](https://github.com/grafana/tempo/blob/main/pkg/tempopb/tempo.proto), [Tempo legacy JSON encoding discussion](https://github.com/grafana/tempo/issues/5437), and [Temporal cancellation](https://docs.temporal.io/develop/python/workflows/cancellation), consulted on 2026-11-14.
