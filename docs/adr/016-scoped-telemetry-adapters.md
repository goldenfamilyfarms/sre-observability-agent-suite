# ADR-016: scoped, bounded read-only telemetry adapters

Status: accepted for the collection pilot; live backend acceptance pending. Date: 2026-11-14.

Decision: implement typed Prometheus matrix, Loki stream and Tempo TraceQL search clients behind an operator-reviewed query policy. Bind the policy and every request to the exact existing service/environment/revision/preview ID/generation. Resolve credential and tenant values only from private collector environment variables. Check preview validity and knowledge version around external reads and optional evidence writes. Keep query templates and tenant authority out of untrusted ticket/model input.

Require all five scope mappings and safely quote selector values. Verify returned metric/log scope labels. Tempo search summaries provide insufficient resource attributes for independent scope verification and are always partial, with explicit query-policy-only attribution. Backend authentication, tenant authorization, query costs and instrumentation truth still require operator controls. Template placeholder presence is not a query authorization parser.

Observations are evidence, not certification results. Empty results, warnings, unsupported values, exceeded limits, stale generations and backend failures cannot become passing gates. Store normalized observations privately, with hashes, context version and an explicit null certification verdict. Evidence persistence is an explicit command option; neither reads nor uploads start an engineering workflow, merge or deploy.

Alternatives rejected: unrestricted model-supplied query URLs would expose credentials and bypass scope; treating HTTP 200 or Tempo search completion as a certification pass would conceal missing/partial data. The draft 120-slot inventory remains unchanged until deterministic release/window-bound assertions and full source attribution are implemented.

Consequences: operators must supply real source mappings and reviewed queries privately. The CLI supports explicit collection today; durable scheduling, total deadlines/cancellation, full trace fetching and executable 40/40/40 certification checks remain open. See the [operator guide](../telemetry-adapters.md), [implementation](../../sre_suite/telemetry/collector.py), [contract tests](../../tests/test_telemetry.py) and [HTTPS tests](../../telemetry_tests/test_https.py).

Follow-up: ADR-017 implements full trace fetching and finite durable scheduling with process deadlines/cancellation in version 0.14. The remaining deterministic certification and live acceptance gates are unchanged.
