# Reviewed demo check-to-scenario mapping

This versioned mapping is specific to the pinned demo implementation. There are 76 applicable slots and 44 probe-backed exclusions. An applicable slot still needs independent verification policy and backend evidence; fixture success is not a PASS. See the [controller guide](demo-fixture-controller.md).

| Check | Concern | Scenario | Applicability / reason |
| --- | --- | --- | --- |
| TRACES-001 | HTTP server span present | `http-journey` | Required; independent telemetry verification |
| TRACES-002 | HTTP method attribute | `http-journey` | Required; independent telemetry verification |
| TRACES-003 | HTTP route uses bounded template | `http-journey` | Required; independent telemetry verification |
| TRACES-004 | HTTP response status attribute | `http-journey` | Required; independent telemetry verification |
| TRACES-005 | HTTP server duration | `http-journey` | Required; independent telemetry verification |
| TRACES-006 | HTTP error span status | `http-journey` | Required; independent telemetry verification |
| TRACES-007 | Outbound HTTP client span | `capabilities` | Pinned Python demo has no outbound_http capability; executed capability probe must confirm absence. |
| TRACES-008 | Outbound HTTP context propagation | `capabilities` | Pinned Python demo has no outbound_http capability; executed capability probe must confirm absence. |
| TRACES-009 | gRPC server span | `capabilities` | Pinned Python demo has no grpc capability; executed capability probe must confirm absence. |
| TRACES-010 | gRPC client span | `capabilities` | Pinned Python demo has no grpc capability; executed capability probe must confirm absence. |
| TRACES-011 | gRPC status mapping | `capabilities` | Pinned Python demo has no grpc capability; executed capability probe must confirm absence. |
| TRACES-012 | Database client span | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| TRACES-013 | Database system attribute | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| TRACES-014 | Database operation attribute | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| TRACES-015 | Database statement redaction | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| TRACES-016 | Cache client span | `capabilities` | Pinned Python demo has no cache capability; executed capability probe must confirm absence. |
| TRACES-017 | Messaging producer span | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| TRACES-018 | Messaging consumer span | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| TRACES-019 | Messaging destination attribute | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| TRACES-020 | Messaging context injection | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| TRACES-021 | Messaging context extraction | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| TRACES-022 | Batch messaging span links | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| TRACES-023 | Asynchronous context propagation | `capabilities` | Pinned Python demo has no async capability; executed capability probe must confirm absence. |
| TRACES-024 | Parent child continuity | `http-journey` | Required; independent telemetry verification |
| TRACES-025 | Cross service trace continuity | `capabilities` | Pinned Python demo has no outbound_http capability; executed capability probe must confirm absence. |
| TRACES-026 | Root span uniqueness | `http-journey` | Required; independent telemetry verification |
| TRACES-027 | Span ID validity | `http-journey` | Required; independent telemetry verification |
| TRACES-028 | Trace ID validity | `http-journey` | Required; independent telemetry verification |
| TRACES-029 | Service name resource | `http-journey` | Required; independent telemetry verification |
| TRACES-030 | Service version resource | `http-journey` | Required; independent telemetry verification |
| TRACES-031 | Environment resource | `http-journey` | Required; independent telemetry verification |
| TRACES-032 | Exception event structure | `http-journey` | Required; independent telemetry verification |
| TRACES-033 | Retry attempt visibility | `capabilities` | Pinned Python demo has no retries capability; executed capability probe must confirm absence. |
| TRACES-034 | Timeout span status | `capabilities` | Pinned Python demo has no timeouts capability; executed capability probe must confirm absence. |
| TRACES-035 | Cancellation visibility | `capabilities` | Pinned Python demo has no cancellation capability; executed capability probe must confirm absence. |
| TRACES-036 | Sampling policy behavior | `http-journey` | Required; independent telemetry verification |
| TRACES-037 | Sensitive attribute exclusion | `invalid-context` | Required; independent telemetry verification |
| TRACES-038 | Span timing consistency | `http-journey` | Required; independent telemetry verification |
| TRACES-039 | Exporter delivery health | `http-journey` | Required; independent telemetry verification |
| TRACES-040 | Synthetic journey completeness | `http-journey` | Required; independent telemetry verification |
| LOGS-001 | Structured record parsing | `http-journey` | Required; independent telemetry verification |
| LOGS-002 | Timestamp field validity | `http-journey` | Required; independent telemetry verification |
| LOGS-003 | Severity field presence | `http-journey` | Required; independent telemetry verification |
| LOGS-004 | Severity mapping correctness | `http-journey` | Required; independent telemetry verification |
| LOGS-005 | Service name identity | `http-journey` | Required; independent telemetry verification |
| LOGS-006 | Service version identity | `http-journey` | Required; independent telemetry verification |
| LOGS-007 | Environment identity | `http-journey` | Required; independent telemetry verification |
| LOGS-008 | Trace ID correlation | `http-journey` | Required; independent telemetry verification |
| LOGS-009 | Span ID correlation | `http-journey` | Required; independent telemetry verification |
| LOGS-010 | Active trace context validity | `http-journey` | Required; independent telemetry verification |
| LOGS-011 | HTTP request context | `http-journey` | Required; independent telemetry verification |
| LOGS-012 | HTTP response context | `http-journey` | Required; independent telemetry verification |
| LOGS-013 | Bounded route field | `http-journey` | Required; independent telemetry verification |
| LOGS-014 | Request correlation continuity | `http-journey` | Required; independent telemetry verification |
| LOGS-015 | gRPC operation context | `capabilities` | Pinned Python demo has no grpc capability; executed capability probe must confirm absence. |
| LOGS-016 | Messaging publish context | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| LOGS-017 | Messaging consume context | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| LOGS-018 | Message correlation continuity | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| LOGS-019 | Batch message context | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| LOGS-020 | Exception type field | `http-journey` | Required; independent telemetry verification |
| LOGS-021 | Exception stack field | `http-journey` | Required; independent telemetry verification |
| LOGS-022 | Exception message redaction | `http-journey` | Required; independent telemetry verification |
| LOGS-023 | Retry attempt field | `capabilities` | Pinned Python demo has no retries capability; executed capability probe must confirm absence. |
| LOGS-024 | Timeout event record | `capabilities` | Pinned Python demo has no timeouts capability; executed capability probe must confirm absence. |
| LOGS-025 | Cancellation event record | `capabilities` | Pinned Python demo has no cancellation capability; executed capability probe must confirm absence. |
| LOGS-026 | Authentication secret exclusion | `invalid-context` | Required; independent telemetry verification |
| LOGS-027 | Personal data redaction | `invalid-context` | Required; independent telemetry verification |
| LOGS-028 | Request body redaction | `invalid-context` | Required; independent telemetry verification |
| LOGS-029 | Database credential exclusion | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| LOGS-030 | Log injection resistance | `invalid-context` | Required; independent telemetry verification |
| LOGS-031 | Multiline event integrity | `http-journey` | Required; independent telemetry verification |
| LOGS-032 | Unicode record integrity | `http-journey` | Required; independent telemetry verification |
| LOGS-033 | Duplicate event control | `http-journey` | Required; independent telemetry verification |
| LOGS-034 | Ingestion latency bound | `http-journey` | Required; independent telemetry verification |
| LOGS-035 | Ingestion loss detection | `http-journey` | Required; independent telemetry verification |
| LOGS-036 | Retention policy evidence | `http-journey` | Required; independent telemetry verification |
| LOGS-037 | Authorized datasource scope | `http-journey` | Required; independent telemetry verification |
| LOGS-038 | Low cardinality labels | `http-journey` | Required; independent telemetry verification |
| LOGS-039 | Synthetic journey log coverage | `http-journey` | Required; independent telemetry verification |
| LOGS-040 | Severity based alert readiness | `http-journey` | Required; independent telemetry verification |
| PROMQL-001 | HTTP request rate | `http-journey` | Required; independent telemetry verification |
| PROMQL-002 | HTTP error rate | `http-journey` | Required; independent telemetry verification |
| PROMQL-003 | HTTP latency histogram | `http-journey` | Required; independent telemetry verification |
| PROMQL-004 | Latency quantile calculation | `http-journey` | Required; independent telemetry verification |
| PROMQL-005 | Route label cardinality | `http-journey` | Required; independent telemetry verification |
| PROMQL-006 | Service identity labels | `http-journey` | Required; independent telemetry verification |
| PROMQL-007 | Environment labels | `http-journey` | Required; independent telemetry verification |
| PROMQL-008 | Artifact attribution evidence | `http-journey` | Required; independent telemetry verification |
| PROMQL-009 | Scrape target availability | `http-journey` | Required; independent telemetry verification |
| PROMQL-010 | Scrape sample freshness | `http-journey` | Required; independent telemetry verification |
| PROMQL-011 | Scrape duration budget | `http-journey` | Required; independent telemetry verification |
| PROMQL-012 | Scrape sample limit headroom | `http-journey` | Required; independent telemetry verification |
| PROMQL-013 | Counter monotonicity and resets | `http-journey` | Required; independent telemetry verification |
| PROMQL-014 | Histogram bucket coverage | `http-journey` | Required; independent telemetry verification |
| PROMQL-015 | Histogram unit consistency | `http-journey` | Required; independent telemetry verification |
| PROMQL-016 | JVM heap utilization | `capabilities` | Pinned Python demo has no jvm capability; executed capability probe must confirm absence. |
| PROMQL-017 | JVM garbage collection duration | `capabilities` | Pinned Python demo has no jvm capability; executed capability probe must confirm absence. |
| PROMQL-018 | JVM thread count | `capabilities` | Pinned Python demo has no jvm capability; executed capability probe must confirm absence. |
| PROMQL-019 | JVM class loading | `capabilities` | Pinned Python demo has no jvm capability; executed capability probe must confirm absence. |
| PROMQL-020 | Process CPU usage | `http-journey` | Required; independent telemetry verification |
| PROMQL-021 | Process resident memory | `http-journey` | Required; independent telemetry verification |
| PROMQL-022 | Process uptime | `http-journey` | Required; independent telemetry verification |
| PROMQL-023 | Process file descriptors | `http-journey` | Required; independent telemetry verification |
| PROMQL-024 | Messaging publish rate | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| PROMQL-025 | Messaging consume rate | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| PROMQL-026 | Messaging error rate | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| PROMQL-027 | Consumer backlog | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| PROMQL-028 | Message processing latency | `capabilities` | Pinned Python demo has no messaging capability; executed capability probe must confirm absence. |
| PROMQL-029 | Database operation rate | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| PROMQL-030 | Database error rate | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| PROMQL-031 | Database latency | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| PROMQL-032 | Connection pool saturation | `capabilities` | Pinned Python demo has no database capability; executed capability probe must confirm absence. |
| PROMQL-033 | Cache hit ratio | `capabilities` | Pinned Python demo has no cache capability; executed capability probe must confirm absence. |
| PROMQL-034 | Exporter queue pressure | `http-journey` | Required; independent telemetry verification |
| PROMQL-035 | Telemetry dropped sample rate | `http-journey` | Required; independent telemetry verification |
| PROMQL-036 | Series cardinality budget | `http-journey` | Required; independent telemetry verification |
| PROMQL-037 | Missing series handling | `http-journey` | Required; independent telemetry verification |
| PROMQL-038 | Availability SLI readiness | `http-journey` | Required; independent telemetry verification |
| PROMQL-039 | Latency SLI readiness | `http-journey` | Required; independent telemetry verification |
| PROMQL-040 | Error budget burn calculation | `http-journey` | Required; independent telemetry verification |
