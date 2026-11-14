"""Reviewed finite-journey calculations. Every recorded series keeps all seven bindings."""

from typing import Any

SCOPE = "service_name,deployment_environment,service_version,preview_id,preview_generation"
BINDING = SCOPE + ",artifact_digest,plan_digest"


def recording_rules() -> dict[str, Any]:
    def total(metric: str) -> str:
        return f"sum by ({BINDING}) ({metric})"

    count = total("sre_demo_requests_total")
    elapsed = total("sre_demo_observation_seconds")
    errors = total("sre_demo_errors_total")
    buckets = f"sum by ({BINDING},le) (sre_demo_request_duration_seconds_bucket)"
    good_latency = total('sre_demo_request_duration_seconds_bucket{le="0.001"}')
    ack = total('sre_demo_export_acknowledged{signal="traces"}')
    ready = f"({count} > 0)"
    # Short finite batch: total / elapsed is the actual rate since this generation's window
    # opened. rate(counter[5m]) would miss an unsampled startup increment.
    calculations = {
        "PROMQL-001": f"{count} / {elapsed}",
        "PROMQL-002": f"{errors} / {elapsed}",
        "PROMQL-004": f"histogram_quantile(0.5, {buckets})",
        "PROMQL-009": 'up{job="sre-demo"} * on(job,instance) group_right sre_demo_target_info',
        "PROMQL-010": "timestamp(sre_demo_target_info)",
        "PROMQL-011": 'scrape_duration_seconds{job="sre-demo"} '
        "* on(job,instance) group_right sre_demo_target_info",
        "PROMQL-012": 'scrape_samples_scraped{job="sre-demo"} / 500 '
        "* on(job,instance) group_right sre_demo_target_info",
        "PROMQL-015": "sre_demo_duration_unit_info",
        "PROMQL-020": "sre_demo_process_cpu_seconds_total",
        "PROMQL-021": "sre_demo_process_resident_memory_bytes",
        "PROMQL-022": "sre_demo_process_uptime_seconds",
        "PROMQL-023": "sre_demo_process_open_fds",
        "PROMQL-034": "sre_demo_outbox_pending",
        "PROMQL-035": f"{total('sre_demo_outbox_missing_items')} / {elapsed}",
        "PROMQL-038": f"1 - ({errors} / {count})",
        "PROMQL-039": f"{good_latency} / {count}",
        "PROMQL-040": f"({errors} / {count}) / 0.01",
        "TRACES-039": f"({ack} == 1)",
        "LOGS-034": 'sre_demo_export_ack_latency_seconds{signal="logs"}',
        "LOGS-036": (
            f"max by ({SCOPE}) (sre_local_loki_retention_seconds) "
            f"* on({SCOPE}) group_right sre_demo_target_info"
        ),
    }
    return {
        "groups": [
            {
                "name": "sre-demo-certification",
                "interval": "1s",
                "rules": [
                    {
                        "record": "sre_demo_check",
                        "expr": f"({expr}) and on({BINDING}) {ready}",
                        "labels": {"check": check},
                    }
                    for check, expr in calculations.items()
                ],
            }
        ]
    }
