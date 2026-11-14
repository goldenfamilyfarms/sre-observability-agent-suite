# Image build, attestation and target-service fixtures

Version 0.26 adds a real BuildKit → OCI archive → registry → authenticated evidence path, plus an exportable containerized Python reference and image-bound fixture preparation. Commands do not merge, deploy, install infrastructure or start certification implicitly. Registry publication requires an explicit command and a current exact-action approval.

## Trusted build worker

Run this worker on a dedicated CI machine with a disposable BuildKit builder, separate builder evidence token, limited registry credential and private durable journal. Do not run arbitrary builds on an engineering worker carrying Jira, model or production credentials. Build steps run as UID/GID 65532 with `--network=none`, no secret/SSH mounts and no privileged entitlements. Base-image retrieval still uses the builder's registry network access. Supply a reviewed digest-pinned base with dependencies already installed, or explicitly vendor dependencies in the approved context. This release supports one Linux amd64 or arm64 image per request.

Private worker JSON contains:

```json
{
  "request": "/private/sre/build-request.json",
  "roles": {"implement":"implementer", "review":"reviewer", "test":"tester", "qa":"qa"},
  "producer": "image-builder",
  "repository": "/private/checkouts/service",
  "journal": "/private/sre/image-journal",
  "tools": {
    "buildx": {"path":"/absolute/path/docker-buildx", "sha256":"REPLACE_WITH_BINARY_SHA256"},
    "skopeo": {"path":"/absolute/path/skopeo", "sha256":"REPLACE_WITH_BINARY_SHA256"},
    "docker_config": "/private/sre/build-docker-config",
    "docker_context": "isolated",
    "builder": "isolated-builder",
    "registry_auth_file": "/private/sre/registry-auth.json",
    "registry_ca": null
  }
}
```

Create the private journal/config directories before use. Use owner-only permissions and physical absolute paths outside Git. Buildx is invoked by its pinned executable path, avoiding ambient Docker plugin selection. The Docker context and named builder must already exist in the private Docker config directory. Pin the BuildKit daemon image when provisioning it. Configure only base-image retrieval credentials there. The publishing credential is in a separate authfile that never enters the build context or process environment.

The registry authfile has exactly `{"auths":{"registry.example.invalid":{"auth":"BASE64_USERNAME_COLON_PASSWORD"}}}`. A short-lived registry access token can be the password where supported by your registry. Credential helpers, extra registry entries and ambient credential fallback are rejected. An empty `auths` object is usable with an explicitly anonymous test registry. For a private CA, set `registry_ca` to `{"path":"/private/sre/registry-trust/ca.crt","sha256":"HEX_SHA256"}`; its directory must contain only that pinned CA. TLS verification remains enabled.

The process also needs its own `SRE_EVIDENCE_API_URL/TOKEN`, `SRE_CONTROL_URL/TOKEN` and optional existing loopback CA variables. The builder subject must differ from all four engineering subjects; the approver must differ from all five. Keep registry permissions limited to the explicit repository and rotate credentials privately.

## Build request and proof

The schema-2 build request contains `binding`, `stages` and `expires_at`. `stages` is the four authenticated evidence references from the independent engineering roles. The binding uses the current preview scope/context version, `artifact_digest=sha256:<implementation evidence SHA>`, the exact engineering `plan_digest`, action `execute`, and this target:

```json
{
  "environment":"development",
  "kind":"image-build",
  "source_repository":"example/service",
  "candidate_revision":"FULL_40_CHARACTER_COMMIT_SHA",
  "chart_revision":"FULL_40_CHARACTER_GITOPS_COMMIT_SHA",
  "chart_digest":"SHA256_FROM_THE_REVIEWED_DELIVERY_CHART",
  "registry_repository":"registry.example.invalid/team/service",
  "recipe": {
    "base_image":"python@sha256:REPLACE_WITH_APPROVED_DIGEST",
    "platform":"linux/amd64",
    "paths":["reference_service.py"],
    "steps":[],
    "entrypoint":["python","/app/reference_service.py"]
  }
}
```

These are placeholders, not a runnable authorization. Approve the exact completed binding through the control service. The candidate must be one commit on the reviewed baseline, and its entire Git tree must equal the independently reviewed implementation edits. The export reads immutable Git objects, not a dirty working tree. Only listed regular files enter the build context; hidden files, supplied Dockerfiles, ignore files, links and submodules cannot become build instructions. The fixed Dockerfile pins the base and entrypoint and applies non-root ownership. Optional build steps are explicit argv lists; they cannot enable network/secret mounts.

```sh
python -m sre_suite.image_build inspect --config /private/sre/builder.json
python -m sre_suite.image_build build --config /private/sre/builder.json --authorize-build \
  > /private/sre/build-result.json
python -m sre_suite.image_build publish --config /private/sre/builder.json --authorize-push \
  > /private/sre/image-receipt.json
```

`publish` requires the completed durable build; it never silently rebuilds source. It verifies OCI descriptors, all referenced blob hashes, platform, runtime recipe and provenance labels. Skopeo preserves the exact manifest digest, then reads that digest back from the registry. The transport tag is derived from the digest; consumers use `repository@sha256:...`. A lost push acknowledgement can retry only the saved immutable archive. Publication and cached receipt reuse recheck current authority, generation, evidence and registry availability. An interrupted build before durable completion can repeat compute; exactly-once build billing is not claimed.

The receipt's `build` and `stages` fields plug directly into the existing delivery request; `image` is the immutable deployment reference. The authenticated `image-build` document binds baseline and candidate commits, implementation evidence, plan, all four stage artifacts, image digest and chart revision/digest. This is evidence authenticated by the suite's builder principal, not a portable Sigstore signature or a claimed SLSA level. Registry storage, isolated CI hosting and credential issuance remain private infrastructure.

Suite graphs now accept local `image-build` and `image-publish` nodes. Their payload is `{config, config_sha256, request_sha256}`, with both hashes covering the corresponding private JSON documents using the suite canonical digest. Use separate node queues and an explicitly started graph. Publication forces a review checkpoint: the newly known image digest needs its own deployment authorization and certification plan.

## Python reference and private targets

```sh
python -m sre_suite.target_service export-reference --output /private/sre/reference-source
```

Review and commit the exported standalone `reference_service.py` into the demo/target repository before producing engineering evidence. It uses only the Python standard library. The service provides `/ready` for cluster probes, authenticated `/sre/identity`, `/health`, `/unicode`, `/error`, and separately authenticated `/metrics`. `/error` executes a controlled exception. It refuses production configuration. TLS terminates at your private ingress; fixture callers require HTTPS.

Delivery supplies the eight `OTEL_RESOURCE_ATTRIBUTES` fields already defined by its chart. The reference additionally needs distinct `SRE_TARGET_TOKEN` and `SRE_TARGET_METRICS_TOKEN` secrets. Configure `SRE_TARGET_TRACES_URL` ending in `/v1/traces` and `SRE_TARGET_LOGS_URL` ending in `/loki/api/v1/push`, with separate `SRE_TARGET_TRACES_AUTH` and `SRE_TARGET_LOGS_AUTH` complete authorization headers. Optional `SRE_TARGET_TRACES_TENANT`, `SRE_TARGET_LOGS_TENANT` and `SRE_TARGET_EXPORT_CA_FILE` are private settings. Use `/ready` as the delivery health path and port 8080 unless explicitly changed.

Request traces use the runner's traceparent, actual timestamps and server span IDs. Structured logs carry the same trace/span IDs and exact resource binding. Prometheus exposes measured request counts/duration sums with bounded route/status/resource labels. A SQLite transaction saves each response and both telemetry outbox entries; repeated request keys do not inflate counters. Export retries retain the same bytes and reject partial trace acknowledgements. The state is bound to one image/plan/preview generation. Its 10,000-request bound fails closed rather than growing indefinitely. Mount persistent storage and choose `--state` when pod-replacement durability is required; the default `/tmp` survives only process restarts within the same storage lifetime. Do not advertise the chart's emptyDir as durable across pod replacement.

## Prepare and run fixtures

After separately authorized preview delivery, obtain fresh independent readiness evidence. `target_service` contains `build` and `readiness` evidence refs, expected `builder` and `delivery` subjects, and `maximum_age_seconds` in 1–300. All six builder/delivery/runner/assessor/collector/verifier subjects must differ. Readiness is refreshable even when pod resource versions are unchanged.

Private fixture preparation JSON contains:

- `plan`: the seven exact fixture binding fields (`release`, `artifact_kind`, `window_start`, `window_end`, `context_version`, `policy_sha256`, `catalog_sha256`), with `artifact_kind=container-image-v1` and the built image digest;
- `verification_policy`: your reviewed 120-check policy, including exact backend queries and source/artifact/plan label mappings;
- `checks`: all 120 independently reviewed `{scenario, expected, justification}` entries, mapped to `health`, `unicode` or `error`;
- `runner`, `runner_queue`, `assessor_queue`;
- `origin`: `{base_url, authorization_env, ca_env}`, using a dedicated `SRE_JOURNEY_*_AUTH` variable and optional loopback-only journey CA;
- `target_service`: the independent evidence/identity configuration described above.

```sh
python -m sre_suite.target_service prepare-fixtures --config /private/sre/target-fixtures.json \
  --output /private/sre/prepared-fixtures
```

This writes `fixture-policy.json` and `fixture-request.json`, validates their scope and digests, and starts nothing. Run them with the existing independent fixture runner/assessor commands and certification workflow. Choose a short observation window (for example 60 seconds), within the readiness freshness budget. Configure scraping/export before the window opens. The runner verifies independent build/readiness evidence and the HTTPS identity endpoint before traffic, and sends an exact resource-binding hash with every workload request. The reference rejects mismatched bindings. The assessor revalidates the proofs and checks responses against the reviewed oracle; it never derives expected values from returned telemetry.

For other services, implement the same identity/binding contract or adapt reviewed journey routes in your private repository. Use `sre.source.revision` for baseline scope and `service.version` for the actual candidate; they are distinct. Scope labels in Loki/Prometheus are the resource attribute names with dots replaced by underscores. Keep unsupported capabilities explicitly justified. This three-route reference does not implement databases, messaging, gRPC or JVM behavior, and does not claim 120 passing checks. Existing 76-pass/44-exclusion local-demo results belong to the earlier demo, not this new service.

## Validation and references

Unit tests cover source/evidence/plan mismatches, full-tree changes, tampered archives, tool pins, credential separation, revoked approval, generation advancement and retry behavior. Real TLS tests cover target identity and outbox acknowledgement/retry. The dedicated `image_build_tests` suite uses pinned BuildKit/base/registry images, an ephemeral local TLS registry and synthetic credentials to build, publish, attest and run the exact reference image. Local `SRE_TEST_BUILD_ONLY=true` deliberately omits registry publication when Skopeo is unavailable; CI runs the complete test on Python 3.11 and 3.14. No private target deployment is performed by these tests.

Provider contracts: [Buildx build/export options](https://docs.docker.com/reference/cli/docker/buildx/build/), [Skopeo digest-preserving copy](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md), [OCI image layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md).
