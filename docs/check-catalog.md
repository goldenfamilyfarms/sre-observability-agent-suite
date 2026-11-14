# Executable check catalog

Catalog `1.0.0` uses service-private source bindings (`bound-observation:${source}`). Every slot has a versioned predicate implementation. Source/field mappings and thresholds must be reviewed for the named concern; see the [operator guide](certification.md). No generic mapping is asserted to certify a live service.

| ID | Concern | Predicate | Required parameters |
| --- | --- | --- | --- |
| TRACES-001 | HTTP server span present | `present.v1` | `path` |
| TRACES-002 | HTTP method attribute | `enum.v1` | `path`, `values` |
| TRACES-003 | HTTP route uses bounded template | `bounded.v1` | `max_length`, `path`, `values` |
| TRACES-004 | HTTP response status attribute | `range.v1` | `maximum`, `minimum`, `path` |
| TRACES-005 | HTTP server duration | `range.v1` | `maximum`, `minimum`, `path` |
| TRACES-006 | HTTP error span status | `mapping.v1` | `pairs`, `source_path`, `target_path` |
| TRACES-007 | Outbound HTTP client span | `present.v1` | `path` |
| TRACES-008 | Outbound HTTP context propagation | `relation.v1` | `child`, `parent` |
| TRACES-009 | gRPC server span | `present.v1` | `path` |
| TRACES-010 | gRPC client span | `present.v1` | `path` |
| TRACES-011 | gRPC status mapping | `mapping.v1` | `pairs`, `source_path`, `target_path` |
| TRACES-012 | Database client span | `present.v1` | `path` |
| TRACES-013 | Database system attribute | `enum.v1` | `path`, `values` |
| TRACES-014 | Database operation attribute | `enum.v1` | `path`, `values` |
| TRACES-015 | Database statement redaction | `excluded.v1` | `forbidden`, `paths` |
| TRACES-016 | Cache client span | `present.v1` | `path` |
| TRACES-017 | Messaging producer span | `present.v1` | `path` |
| TRACES-018 | Messaging consumer span | `present.v1` | `path` |
| TRACES-019 | Messaging destination attribute | `bounded.v1` | `max_length`, `path`, `values` |
| TRACES-020 | Messaging context injection | `relation.v1` | `child`, `parent` |
| TRACES-021 | Messaging context extraction | `relation.v1` | `child`, `parent` |
| TRACES-022 | Batch messaging span links | `links.v1` | none |
| TRACES-023 | Asynchronous context propagation | `relation.v1` | `child`, `parent` |
| TRACES-024 | Parent child continuity | `graph.v1` | none |
| TRACES-025 | Cross service trace continuity | `coverage.v1` | `path` |
| TRACES-026 | Root span uniqueness | `roots.v1` | none |
| TRACES-027 | Span ID validity | `hex_id.v1` | `length`, `path` |
| TRACES-028 | Trace ID validity | `hex_id.v1` | `length`, `path` |
| TRACES-029 | Service name resource | `equals.v1` | `path`, `value` |
| TRACES-030 | Service version resource | `equals.v1` | `path`, `value` |
| TRACES-031 | Environment resource | `equals.v1` | `path`, `value` |
| TRACES-032 | Exception event structure | `event.v1` | `attributes`, `name` |
| TRACES-033 | Retry attempt visibility | `range.v1` | `maximum`, `minimum`, `path` |
| TRACES-034 | Timeout span status | `enum.v1` | `path`, `values` |
| TRACES-035 | Cancellation visibility | `enum.v1` | `path`, `values` |
| TRACES-036 | Sampling policy behavior | `coverage.v1` | `path` |
| TRACES-037 | Sensitive attribute exclusion | `excluded.v1` | `forbidden`, `paths` |
| TRACES-038 | Span timing consistency | `timing.v1` | `maximum_clock_skew_ns` |
| TRACES-039 | Exporter delivery health | `range.v1` | `maximum`, `minimum`, `path` |
| TRACES-040 | Synthetic journey completeness | `coverage.v1` | `path` |
| LOGS-001 | Structured record parsing | `structured.v1` | none |
| LOGS-002 | Timestamp field validity | `timestamp.v1` | `path` |
| LOGS-003 | Severity field presence | `present.v1` | `path` |
| LOGS-004 | Severity mapping correctness | `mapping.v1` | `pairs`, `source_path`, `target_path` |
| LOGS-005 | Service name identity | `equals.v1` | `path`, `value` |
| LOGS-006 | Service version identity | `equals.v1` | `path`, `value` |
| LOGS-007 | Environment identity | `equals.v1` | `path`, `value` |
| LOGS-008 | Trace ID correlation | `hex_id.v1` | `length`, `path` |
| LOGS-009 | Span ID correlation | `hex_id.v1` | `length`, `path` |
| LOGS-010 | Active trace context validity | `correlation.v1` | `span_path`, `trace_path` |
| LOGS-011 | HTTP request context | `present.v1` | `path` |
| LOGS-012 | HTTP response context | `range.v1` | `maximum`, `minimum`, `path` |
| LOGS-013 | Bounded route field | `bounded.v1` | `max_length`, `path`, `values` |
| LOGS-014 | Request correlation continuity | `coverage.v1` | `path` |
| LOGS-015 | gRPC operation context | `present.v1` | `path` |
| LOGS-016 | Messaging publish context | `present.v1` | `path` |
| LOGS-017 | Messaging consume context | `present.v1` | `path` |
| LOGS-018 | Message correlation continuity | `coverage.v1` | `path` |
| LOGS-019 | Batch message context | `range.v1` | `maximum`, `minimum`, `path` |
| LOGS-020 | Exception type field | `present.v1` | `path` |
| LOGS-021 | Exception stack field | `present.v1` | `path` |
| LOGS-022 | Exception message redaction | `excluded.v1` | `forbidden`, `paths` |
| LOGS-023 | Retry attempt field | `range.v1` | `maximum`, `minimum`, `path` |
| LOGS-024 | Timeout event record | `enum.v1` | `path`, `values` |
| LOGS-025 | Cancellation event record | `enum.v1` | `path`, `values` |
| LOGS-026 | Authentication secret exclusion | `excluded.v1` | `forbidden`, `paths` |
| LOGS-027 | Personal data redaction | `excluded.v1` | `forbidden`, `paths` |
| LOGS-028 | Request body redaction | `excluded.v1` | `forbidden`, `paths` |
| LOGS-029 | Database credential exclusion | `excluded.v1` | `forbidden`, `paths` |
| LOGS-030 | Log injection resistance | `control_chars.v1` | `path` |
| LOGS-031 | Multiline event integrity | `structured.v1` | none |
| LOGS-032 | Unicode record integrity | `unicode.v1` | `path` |
| LOGS-033 | Duplicate event control | `unique.v1` | `path` |
| LOGS-034 | Ingestion latency bound | `latency.v1` | `maximum_seconds`, `path` |
| LOGS-035 | Ingestion loss detection | `coverage.v1` | `path` |
| LOGS-036 | Retention policy evidence | `range.v1` | `maximum`, `minimum`, `path` |
| LOGS-037 | Authorized datasource scope | `equals.v1` | `path`, `value` |
| LOGS-038 | Low cardinality labels | `cardinality.v1` | `maximum`, `path` |
| LOGS-039 | Synthetic journey log coverage | `coverage.v1` | `path` |
| LOGS-040 | Severity based alert readiness | `enum.v1` | `path`, `values` |
| PROMQL-001 | HTTP request rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-002 | HTTP error rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-003 | HTTP latency histogram | `buckets.v1` | `bounds`, `minimum_samples` |
| PROMQL-004 | Latency quantile calculation | `metric_expected.v1` | `minimum_samples`, `tolerance` |
| PROMQL-005 | Route label cardinality | `cardinality.v1` | `maximum`, `path` |
| PROMQL-006 | Service identity labels | `equals.v1` | `path`, `value` |
| PROMQL-007 | Environment labels | `equals.v1` | `path`, `value` |
| PROMQL-008 | Artifact attribution evidence | `equals.v1` | `path`, `value` |
| PROMQL-009 | Scrape target availability | `available.v1` | `minimum_samples` |
| PROMQL-010 | Scrape sample freshness | `sample_age.v1` | `maximum_seconds`, `minimum_samples` |
| PROMQL-011 | Scrape duration budget | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-012 | Scrape sample limit headroom | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-013 | Counter monotonicity and resets | `monotonic.v1` | `maximum_resets`, `minimum_samples` |
| PROMQL-014 | Histogram bucket coverage | `buckets.v1` | `bounds`, `minimum_samples` |
| PROMQL-015 | Histogram unit consistency | `equals.v1` | `path`, `value` |
| PROMQL-016 | JVM heap utilization | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-017 | JVM garbage collection duration | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-018 | JVM thread count | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-019 | JVM class loading | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-020 | Process CPU usage | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-021 | Process resident memory | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-022 | Process uptime | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-023 | Process file descriptors | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-024 | Messaging publish rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-025 | Messaging consume rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-026 | Messaging error rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-027 | Consumer backlog | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-028 | Message processing latency | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-029 | Database operation rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-030 | Database error rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-031 | Database latency | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-032 | Connection pool saturation | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-033 | Cache hit ratio | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-034 | Exporter queue pressure | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-035 | Telemetry dropped sample rate | `metric_range.v1` | `maximum`, `minimum`, `minimum_samples` |
| PROMQL-036 | Series cardinality budget | `cardinality.v1` | `maximum`, `path` |
| PROMQL-037 | Missing series handling | `coverage.v1` | `path` |
| PROMQL-038 | Availability SLI readiness | `metric_expected.v1` | `minimum_samples`, `tolerance` |
| PROMQL-039 | Latency SLI readiness | `metric_expected.v1` | `minimum_samples`, `tolerance` |
| PROMQL-040 | Error budget burn calculation | `metric_expected.v1` | `minimum_samples`, `tolerance` |
