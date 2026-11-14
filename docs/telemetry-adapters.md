# Scoped telemetry collection

Version 0.13 implements Prometheus range queries with float matrix results, Loki log-range queries with structured metadata, and Tempo TraceQL search summaries. `TelemetryCollector` is the supported scoped entry point; `TelemetryAdapter` is the lower-level protocol client for trusted callers. Neither returns a gate verdict or starts a workflow. These adapters are not yet scheduled by the engineering Temporal workflow.

## Private setup

Start from [the synthetic policy](../examples/telemetry-policy.json) and [request](../examples/telemetry-request.json). Copy them into an operator-owned private directory outside Git, set files to mode `0600` and the directory to `0700`. Replace the scope in **both** files with the registered pilot's exact service, environment, full source revision, preview ID and generation. A generation replacement requires a new reviewed policy and matching evidence API grants; the command never registers or advances a preview.

The policy is trusted operator configuration, not agent-generated input or a Jira attachment. It authorizes named profiles for one exact scope. Review the entire query template and its source mappings before use. The request can select an approved profile and bounded time window; it cannot supply an endpoint, query, headers, tenant or credentials. Keep policy files and telemetry outputs out of the service checkout and model context.

Each profile has an HTTPS gateway base URL, a backend, an authorization environment variable, an optional tenant environment variable, a query template and five scope mappings. All five source dimensions must exist. The `${scope}` placeholder expands to exact-match selectors with JSON-quoted values; Tempo uses resource attributes. Generation is a **string** in all selectors, so configure instrumentation accordingly. Templates must contain exactly one placeholder. Prometheus aggregations must retain all five labels; missing or mismatched labels in returned series/log streams are rejected. Source label equality is checked, not the truth of instrumentation claims.

Templates are not parsed as a complete PromQL/LogQL/TraceQL authorization language. A trusted operator must reject templates that introduce unscoped subqueries, broad unions or expensive expressions. Use backend read-only credentials, tenant isolation, gateway query limits and egress policy as the actual authorization boundary. Tempo search summaries lack all resource attributes, so their attribution is explicitly `query-policy-only`. They cannot establish release identity or full trace coverage. Queries and logs remain untrusted evidence, never executable instructions.

Configure these variables privately in the collector process:

| Variable | Value |
| --- | --- |
| `SRE_EVIDENCE_API_URL` | Current HTTPS evidence API |
| `SRE_EVIDENCE_API_TOKEN` | Exact-scope context grant; add evidence upload/read grants only when uploading |
| `SRE_EVIDENCE_CA_FILE` | Optional absolute CA file for the existing loopback pilot evidence API |
| `SRE_TELEMETRY_PROMETHEUS_AUTH` | Complete `Bearer …` or `Basic …` authorization header |
| `SRE_TELEMETRY_LOKI_AUTH` | Complete read-only authorization header |
| `SRE_TELEMETRY_TEMPO_AUTH` | Complete read-only authorization header |
| `SRE_TELEMETRY_LOKI_TENANT` / `SRE_TELEMETRY_TEMPO_TENANT` | Single tenant for the example profiles; set `tenant_env` to null if not needed |

For Basic authentication, privately base64-encode `username:token` and prefix it with `Basic `. Do not put credentials in the policy, query string, shell history or repository. Backend URLs use HTTPS port 443 with system trust, optional gateway path prefixes, no userinfo, query or fragment. Private CAs can be installed in the host trust store; telemetry configuration cannot disable verification. The existing loopback CA override applies to the evidence API only. Telemetry variables are deliberately not forwarded to Claude Code or the current local-pilot worker configuration.

## Explicit invocation

Update the request to a recent UTC time window before running. Select `prometheus-up`, `loki-logs` or `tempo-traces` from the example policy. Use a new output directory per observation:

```sh
python -m sre_suite.telemetry \
  --policy /absolute/private/telemetry-policy.json \
  --request /absolute/private/telemetry-request.json \
  --output /absolute/private/observations/run-001
```

This reads scoped context and the telemetry backend, then writes a private `observation.json`. Add `--upload-evidence` to explicitly persist the observation through the evidence API and emit `evidence.json`. Output files are exclusive, owner-only and atomically published outside Git. The evidence service assigns the producer identity and the client verifies returned bytes. The command prints only status, static issue codes and whether evidence was uploaded; it does not print query text, log lines, endpoints or backend diagnostics. Exit 0 means an observation was obtained, **not** that a check passed; exit 2 means empty/partial, and exit 1 means failure.

The collector checks the current preview and knowledge version before and after the source call, before upload, and after upload verification. API writes also enforce the current preview transactionally. If a generation changes after a successful upload, historical evidence may remain in storage, but collection cannot report success for that stale scope. File publication and API upload are not one transaction; a filesystem failure after upload can leave evidence without a local bundle. Re-running queries obtains a new observation and may return different data. There are no automatic retries or source fallbacks.

## Bounds and result meaning

- Explicit positive window, at most one hour, wholly within the past 24 hours. Prometheus bounds are inclusive; Loki end is exclusive. Tempo requires whole-second bounds and may return a trace whose root starts before the window because a matching span overlaps it.
- Request limit 1–500, Prometheus step 1–3600 seconds, at most 5,000 float samples, bounded labels and 16 KiB log lines. Limits reached are partial; responses exceeding the requested budgets are rejected, never silently truncated. No pagination is attempted.
- GET-only source endpoints, TLS verification, redirect refusal, 20-second socket timeout and a 15-second Prometheus evaluation timeout. This is not an end-to-end wall-clock deadline; future durable activity scheduling must add a total deadline and cancellation budget. Backend cost limits remain an operator responsibility.
- Common HTTP transport caps response bytes at 1.5 MB and JSON at 10,000 nodes / depth 16. The adapter adds a 500 KB canonical payload cap and collection caps evidence at 1 MB. Unsupported native histograms, Loki metric responses, nonfinite samples, malformed timestamps, duplicate series/traces and mismatched scope labels fail closed.
- Warnings/infos and saturated limits yield `partial`. Zero Prometheus/Loki records yield `empty`. Tempo search is **always partial**, even with zero results: it is a nondeterministic bounded search, not a completeness proof. Search summaries are not complete OTLP traces. Full trace fetching, deterministic trace assertions and telemetry pipeline checks remain subsequent work.

Evidence contains the request, reviewed profile, rendered query, query and canonical-response SHA-256 digests, normalized typed results, context version and observation time. It expires after 15 minutes; API scope/expiry checks still apply. Unknown backend fields are not normalized into evidence, and warning text is not forwarded. The response hash identifies the canonical decoded response, not raw network bytes. No artifact/plan binding or factual verification is inferred from a scope selector. The 120-slot certification catalog still has no executable check definitions and cannot pass.

## Validation and protocol references

Run `python -m unittest tests.test_telemetry -v` for parsing, budgets, encoding, scope checks, private CLI output and the real authenticated SQLite evidence API. Run `python -m unittest discover -s telemetry_tests -v` for real disposable loopback TLS requests, headers, certificate trust, redirect refusal, HTTP errors, duplicate JSON and response byte limits. The latter requires OpenSSL and permission to bind a loopback port. Neither suite uses actual backend accounts.

Protocol behavior follows the official [Prometheus range-query API](https://prometheus.io/docs/prometheus/latest/querying/api/#range-queries), [Loki range-query API](https://grafana.com/docs/loki/latest/reference/loki-http-api/#query-logs-within-a-range-of-time), and [Tempo search API](https://grafana.com/docs/tempo/latest/api_docs/#search), checked on 2026-11-14. Live acceptance still requires your privately configured gateways, instrumentation mappings, permissions and representative service data.
