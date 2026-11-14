# Reference deployment package and image certification

Version 0.27 adds an optional, exact-action-bound `reference_service` profile to the existing delivery target. It generates a reviewable package; preparing it never creates a Kubernetes resource, publishes a secret, starts a workflow or deploys an image.

The profile is for the containerized Python reference only: development/staging, one replica, port 8080, and `/ready` probes. It retains the independent builder/stage evidence, image and plan digests, preview ID/generation, chart Git revision and current human/automatic authorization checks. An old approval cannot authorize an added or edited profile.

## Private configuration

Add this field to `binding.target` in the existing complete schema-2 delivery request:

```json
"reference_service": {
  "storage_class": "private-csi",
  "storage_gib": 1,
  "ingress_class": "private-ingress",
  "hostname": "reference.example.invalid",
  "tls_secret": "reference-tls",
  "credentials_secret": "reference-credentials",
  "export_ca_secret": "export-ca",
  "monitor_labels": {"release": "private-prometheus"}
}
```

Supply these exact `runtime_environment` entries, plus the two actual HTTPS export endpoints:

```json
[
  {"name":"SRE_TARGET_STATE","value":"/state/reference.db"},
  {"name":"SRE_TARGET_EXPORT_CA_FILE","value":"/trust/ca.crt"},
  {"name":"SRE_TARGET_TOKEN","valueFrom":{"secretKeyRef":{"name":"reference-credentials","key":"workload-token"}}},
  {"name":"SRE_TARGET_METRICS_TOKEN","valueFrom":{"secretKeyRef":{"name":"reference-credentials","key":"metrics-token"}}},
  {"name":"SRE_TARGET_TRACES_AUTH","valueFrom":{"secretKeyRef":{"name":"reference-credentials","key":"traces-authorization"}}},
  {"name":"SRE_TARGET_LOGS_AUTH","valueFrom":{"secretKeyRef":{"name":"reference-credentials","key":"logs-authorization"}}},
  {"name":"SRE_TARGET_TRACES_URL","value":"https://tempo.example.invalid/v1/traces"},
  {"name":"SRE_TARGET_LOGS_URL","value":"https://loki.example.invalid/loki/api/v1/push"}
]
```

The private namespace needs the referenced TLS Secret, credentials Secret and export CA Secret (`ca.crt`). Workload and metrics tokens must be distinct and at least 32 characters. Store raw tokens for workload/metrics, and complete authorization header values for the exporters. The CA bundle must trust both export endpoints. No Secret values are generated or embedded in the package. Secret provisioning and rotation belong to private infrastructure.

```sh
python -m sre_suite.target_service prepare-deployment \
  --config /private/sre/reference-delivery-request.json \
  --output /private/sre/reference-deployment-package
```

`deployment-package.json` contains the original request and exact binding, rendered resources, Argo Application, resource digest and `execution_started=false`. It refuses an existing output directory. Review the package, provision prerequisites and approve the exact request separately; the existing delivery commands retain explicit start/authorization requirements.

## Resource behavior and prerequisites

- The Deployment uses non-root UID/GID 65532, an fsGroup, a read-only root filesystem and `Recreate` updates. A `ReadWriteOncePod` PVC provides `/state`, while the exporter CA mounts read-only at `/trust`.
- The claim name hashes the target scope, image and plan. Container/pod replacement within the same release retains SQLite request keys, counters and telemetry outbox. A different release binding uses a different claim. Old claims are not silently reused or pruned. Explicit namespace cleanup may delete claims: storage retention, backup and reclaim policy must be reviewed privately.
- A TLS Ingress uses the exact host, ingress class and pre-existing certificate Secret. The selected controller must be configured for **internal exposure and HTTPS enforcement** in the private environment. The generic Ingress API does not guarantee either through its class name alone.
- A ServiceMonitor selects the owned Service and uses a Secret-backed Bearer token, disables redirects and scrapes `/metrics` every five seconds. This scrape is HTTP inside the cluster; workload access through ingress is HTTPS. If the private cluster requires encrypted pod-to-pod scrape traffic, supply a reviewed mesh/TLS integration before enabling this profile.
- The cluster needs an installed Prometheus Operator with ServiceMonitor discovery, an ingress controller, and a CSI storage class supporting ReadWriteOncePod and fsGroup. This package does not install operators or provision GKE.
- Readiness still verifies actual image IDs and the pod recipe. For this profile it additionally checks ownership and the reviewed Service/PVC/Ingress/ServiceMonitor specs, PVC binding and Recreate strategy. Resource existence does not prove external DNS, certificate trust, ingress reachability or successful scraping; the live fixture/telemetry run tests those paths.

Schema references: [Kubernetes persistent volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/), [Prometheus Operator ServiceMonitor authentication](https://prometheus-operator.dev/docs/api-reference/api/).

## Full image-to-report integration

`python -m unittest discover -s image_build_tests -v` requires Docker/Buildx, Skopeo, OpenSSL and the pinned telemetry images from `sre_suite.local_telemetry.config.IMAGES`. CI runs it on Python 3.11 and 3.14. The complete test refuses build-only mode.

The test builds the reviewed standalone source, publishes to a disposable TLS registry, verifies the manifest digest and builder evidence, and runs the exact OCI image. Independent fixture runner/assessor identities drive real HTTPS requests; the service exports actual traces and logs and Prometheus scrapes actual counters. The container is replaced with its persistent volume and explicit HTTP retries replay stable workload keys and durable worker journals recover the same measurements. The collector uploads real backend captures and the verifier produces all 120 results (40 per category), including explicit inconclusive outcomes for unmeasured concerns. There are no imported demo capability exclusions or fabricated telemetry measurements.

This is a complete **image-to-certification report** test, not a claim of 120 passing checks. Its reviewed reference oracle checks the implemented HTTP/resource/correlation/counter behaviors and requires the final gate to remain blocked where measurements are absent. Negative cases cover artifact/plan substitution, preview ID/generation substitution, missing telemetry and evidence reuse after generation advancement. Certificate journal replay must return the same receipt for the unchanged request.

The integration uses modeled GKE/Argo responses and a local TLS ingress stand-in; it does not deploy to any cluster. Docker, the image registry, container replacement, telemetry backends, fixture execution and certification engine are real. Private CSI, ingress, Prometheus Operator, GKE/Argo rollout and recovery acceptance remain necessary.

## Complete journey traces

The runner must export its measured client spans as well as the service's server spans. Add this optional field to the private `prepare-fixtures` configuration to enable the complete trace path:

```json
"journey_export": {
  "base_url": "https://tempo.example.invalid",
  "authorization_env": "SRE_JOURNEY_TRACES_AUTH",
  "ca_env": null
}
```

Provide a dedicated OTLP write credential in `SRE_JOURNEY_TRACES_AUTH` to the runner only. The assessor never receives this token. Loopback tests may use `SRE_JOURNEY_TRACE_CA_FILE`; private endpoints use the normal trust store. The exporter sends to `/v1/traces`, refuses redirects and rejects partial/warning acknowledgements. Its full configuration is covered by the fixture policy fingerprint and dispatch checks.

Client spans measure actual HTTP call boundaries and reuse the exact parent IDs sent to the target. A root envelope covers the measured journey interval. Runner resource identity is `sre-fixture-runner`, distinct from the target service; all artifact/plan/preview attributes remain intact. The independent collector still queries and authenticates the complete trace. No service span is synthesized or rewritten. Export receipts and client measurements use the existing durable journal; retry after a lost acknowledgement sends identical span bytes and rechecks generation/authority. Without this opt-in, missing parent spans correctly leave full-trace checks inconclusive.
