# Local Prometheus, Loki and Tempo pilot

Version 0.21 adds an explicitly started, single-host telemetry stack for the [managed demo pilot](managed-demo-pilot.md). Prometheus scrapes the target's authenticated HTTPS `/metrics` endpoint every second. The existing durable outbox exports logs and traces through separate TLS gateways to Loki and Tempo. Collectors query those gateways using distinct read credentials and retain the existing artifact, plan, preview ID, generation, window and evidence-context checks.

The stack uses Prometheus v3.14.0, Loki 3.6.5 and Tempo 2.9.5 pinned by multi-platform image digest in `sre_suite/local_telemetry/config.py`. Initialization neither downloads images nor starts services. Startup requires cached images and an explicit flag; it does not start a workflow, provision cloud resources, merge or deploy.

## Prepare privately

Use Python 3.11+, OpenSSL and a local Docker daemon. Linux Docker Engine uses `native` mode; macOS Docker Desktop uses `desktop` mode. Docker Desktop routes container-to-host requests through `host.docker.internal`; TLS still verifies the target certificate against `localhost`. Native Linux uses host networking with every backend listener bound to loopback. Remote Docker contexts are rejected. Windows is not supported.

First prepare the existing [pilot manifest](pilot-setup.md) and [managed target policy](managed-demo-pilot.md), including the target certificate/key and three distinct fixture/metrics/lifecycle credentials. Save the complete metrics Bearer header in a mode-0600 file. All input/output paths must be physical absolute paths in owner-only directories outside Git. The target may remain stopped during preparation.

```sh
python -m sre_suite.local_telemetry init \
  --directory /absolute/private/telemetry \
  --manifest /absolute/private/pilot-manifest.json \
  --target https://localhost:9443 \
  --target-ca /absolute/private/target.crt \
  --metrics-token-file /absolute/private/metrics-token \
  --mode desktop
```

Use `--mode native` on Linux. The target must be an exact loopback HTTPS origin with an explicit unprivileged port. Production scope is rejected. For port conflicts, supply `--ports-file` containing a private JSON object with **all** keys below and distinct unprivileged integer ports. Initialization refuses to overwrite an existing directory.

| Purpose / key | Default port |
| --- | --- |
| `prometheus`, `loki`, `tempo`, `otlp` | 19090, 13100, 13200, 14318 |
| `loki_grpc`, `tempo_grpc`, `memberlist` | 19096, 19095, 17946 |
| `prometheus_tls`, `loki_tls`, `tempo_tls` | 19443, 19444, 19445 |

Initialization writes private backend configurations, a seven-day localhost certificate/key, five independent gateway Bearer credentials, and three persistent data directories. The target CA and metrics token are copied into the bundle. Treat the bundle as immutable; a changed generated backend configuration or image pin is rejected. Certificate rotation or version migration requires a separately reviewed bundle/run and deliberate retained-data migration; it is not automatic.

## Connect the existing workers

Generated files contain the following exact values to integrate into private configuration:

| File | Consumer |
| --- | --- |
| `export.json` | Copy this object to `export` in the managed demo policy before validating and freezing its dispatch |
| `export-env.json` | Add these three variables to the lifecycle worker's private environment/credentials file: two ingest headers and `SRE_DEMO_EXPORT_CA_FILE` |
| `collector-env.json` | Add these four variables to the collector worker environment: three read headers and `SRE_TELEMETRY_CA_FILE` |
| `query-policy.json` | Narrow journey policy for manual collection; use its origins, credential names, CA override and seven scope/binding labels when reviewing private verification sources |

Do not source JSON as shell code or print its secrets. Keep worker credentials separate. The lifecycle worker also needs its existing evidence, fixture, metrics and readiness credentials; generated export credentials do not replace them. The runner/assessor/verifier continue to use their independent identities. Prometheus receives only the target CA and metrics token, never the gateway credentials or private TLS key.

The generated query policy reads `sre_demo_requests_total`, scoped Loki logs and full Tempo traces. It is **not** a complete 120-check verification policy. Preserve reviewed thresholds, field mappings, fixed trace identity, independent fixture expectations and all catalog slots when adapting the existing certification policy. Do not manufacture passing values from returned telemetry. Changed endpoints or CA configuration change policy fingerprints and require regeneration/review of fixture, managed-demo and engineering dispatches before a new run. Legacy remote profiles omit `ca_env` and retain their existing serialization/fingerprints.

## Pull and start explicitly

Inspect the pins with `python -m sre_suite.local_telemetry images`. To fetch only those images:

```sh
python - <<'PY'
import subprocess
from sre_suite.local_telemetry.config import IMAGES
for image in IMAGES.values():
    subprocess.run(['docker', 'pull', image], check=True, timeout=180)
PY
python -m sre_suite.local_telemetry up \
  --directory /absolute/private/telemetry --authorize-start
```

Keep this foreground process running alongside the existing [PostgreSQL/Temporal/evidence API supervisor](local-pilot.md) and dedicated managed-demo, runner, assessor, collector and verifier workers. A redacted `ready` record confirms backend readiness through authenticated TLS. It does not assert target scrape success: the managed target starts only when explicitly dispatched. Run the existing handoff/preflight and explicit workflow-start sequence after reviewing the immutable execution inputs. Start the telemetry stack before the observation window and reserve enough startup/window/settlement time for scrapes and backend ingestion. Empty, delayed or mismatched results remain non-passing observations.

Ctrl-C or SIGTERM closes gateways and removes only containers whose name and ownership labels match this bundle; data remains. After an interrupted supervisor, explicitly clean up its owned containers before restarting:

```sh
python -m sre_suite.local_telemetry stop \
  --directory /absolute/private/telemetry --authorize-stop
```

`stop` refuses an actively locked supervisor. Stop that foreground process first. No command removes retained data. Repeated `up` uses the same private bind mounts and refuses to adopt running or unrelated containers. Prometheus retains up to 24 hours/256 MB; Tempo block retention is 24 hours. Version 0.23 also enables Loki's 24-hour compactor retention with a filesystem delete-request store. The authenticated retention probe checks effective configuration; a short test does not demonstrate elapsed deletion. Existing bundles require explicit regeneration; see [migration details](demo-instrumentation.md). Use bounded pilot runs, monitor available disk and deliberately retire the private bundle after evidence retention requirements are met.

## Trust boundary and acceptance

This is a trusted single-host pilot. Raw backend HTTP ports are loopback-only on the host; local processes and Docker administrators can bypass the TLS gateway. Docker Desktop containers also share its bridge network. This is not a multi-tenant security boundary or a production observability installation. The HTTPS gateways enforce fixed routes, separate read/ingest authorization, certificate verification, bounded messages and redirect refusal; they do not provide per-preview database tenancy. Exact scope and candidate checks remain in the authenticated suite collectors and evidence API. Each bundle is intended for one reviewed preview generation.

The end-to-end test runs real Temporal, the authenticated evidence API, the managed HTTPS demo subprocess and all three real backends. It checks six scraped metric series, six correlated logs, seven trace spans, evidence uploads, artifact/plan isolation, preview/generation guards, wrong-role rejection, complete backend restart and recovered observations. It uses disposable synthetic identities and no Jira/model credentials. Run it after the explicit image pull:

```sh
python -m pip install '.[orchestration]'
python -m unittest discover -s backend_pilot_tests -v
```

CI runs this test on Linux/Python 3.11 and 3.14; local validation also exercises macOS Docker Desktop. This proves the local fixture-to-backend-to-evidence path, not a passing service certificate or live Jira/Claude execution. Cross-host recovery, arbitrary candidate deployment, backend upgrades, cloud provisioning and private service acceptance remain separate work.

Configuration references: [Prometheus scrape/TLS configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/), [Loki filesystem examples](https://grafana.com/docs/loki/latest/configure/examples/configuration-examples/), [Tempo configuration](https://grafana.com/docs/tempo/latest/configuration/) and [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/). Runtime tests validate the pinned versions; future documentation defaults do not automatically upgrade the bundle.

To connect an already reviewed full 120-check policy to this stack, use the [full certification preparation guide](local-certification.md). The narrow generated journey query policy remains insufficient by itself.
