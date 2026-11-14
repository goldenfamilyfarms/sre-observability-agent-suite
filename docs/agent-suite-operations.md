# Suite agents and private environment operation

The suite now provides callable implementations for the twelve proposal roles: Intake/Architecture, Assessment, Planning, Implementation, Code Review, QA, Grafana Query, Verification, Shipping, Deployment/Readiness, Remediation/Regression and Knowledge Steward. The supporting standards, synthetic journey and pipeline-health code is included. This is a bounded v1 implementation. Provider credentials and service-specific mappings are private inputs; live provider acceptance is a separate requirement.

No command runs on import. Starting a worker only subscribes to its configured queue. Workflow start is explicit. A green test suite does not certify an organization's services, grant deployment authority, or establish production operational readiness.

## Agent entrypoints

| Role | Implementation / command | Output and stopping behavior |
| --- | --- | --- |
| Intake / Architecture | `python -m sre_suite.discovery`; `operations --request ...` with `role=topology` | Exact Git source inventory, static findings, runtime parent/span edges with citations; missing edges remain unknown |
| Assessment / standards | `python -m sre_suite.operations --request PRIVATE.json --journal PRIVATE_DIR` | `role=standards` produces hash/line/AST citations; `role=assessment` combines independently authenticated standards, topology and pipeline-health reports into a bounded maturity score and gap register |
| Planning | `python -m sre_suite.jira_planning` | Assessed dependency DAG, `epic-inspect`, `epic-publish`, authenticated ticket publication and preflight handoff |
| Implementation / Review / QA | `python -m sre_suite.execution.cli worker` with role configuration | Separate Claude Code subjects, deterministic test worker, candidate-bound stage chain, draft PR and Jira review transition |
| Grafana Query | `python -m sre_suite.telemetry` | Scoped Prometheus/Loki/Tempo reads, full trace retrieval, finite durable schedules |
| Verification | `python -m sre_suite.verification`; `python -m sre_suite.fixtures` | Independent fixture runner/assessor and 120 checks; accepts explicitly distinguished source-candidate and container-image artifacts |
| Shipping | `python -m sre_suite.delivery shipping-inspect --config PRIVATE.json`; `shipping-merge --authorize-merge` | Complete Git tree comparison with implementation evidence, independent review and trusted CI app checks, exact fast-forward with an atomic base-ref lease |
| Deployment / Readiness | `python -m sre_suite.delivery prepare`, `worker`, `start --authorize-start`, `readiness` | GKE identity and endpoint checks, Helm rendering, owned Argo CD Application, actual deployment/pod image checks, readiness evidence, separately approved rollback/cleanup |
| Remediation / Regression | `operations` with `role=regression` or `role=remediation`; `coordination` | Full 120-slot comparison, proposed remediation DAG linked to the source report; no gate weakening. At most two separately reviewed correction attempts |
| Knowledge Steward | `python -m sre_suite.control steward --request PRIVATE.json --output PRIVATE_DIR` | Verify explicit trusted evidence assertions; conflicts, supersession, expiry, audit and acknowledged outbox delivery |
| Suite orchestration | `python -m sre_suite.coordination inspect/worker/start --config PRIVATE.json` | Frozen dependency graph, independently queued workers, finite attempts, explicit checkpoints and cancellation propagation |

The assessment score has a maximum of two: one point for all configured static controls being observed and one for the required runtime topology and health observations passing. It is a prioritization measure, not certification. Standards indicators can match comments or unused imports; AST citations and runtime findings are reported separately. Framework-specific recommendations and private service assertions still require engineering review.

## Private configuration and identities

Use owner-only files and directories outside Git, with absolute physical paths. Do not paste organization inventories, credentials, certificates, production project names or evidence into the public repository. Examples below contain only generic names and placeholders. Commands reject incomplete configuration.

Install `.[postgres,orchestration]`, run the documented PostgreSQL migrations (now versions 1–3), and start the existing local PostgreSQL/Temporal package. For the expanded suite, serve `sre_suite.control` over HTTPS using the same migrated database. It includes the existing evidence/context routes plus authenticated control routes. Point both `SRE_EVIDENCE_API_URL` and `SRE_CONTROL_URL` at that service. The original local pilot evidence host remains supported for the earlier, non-production pilot.

Control server configuration has exactly these keys:

```json
{
  "credentials": "/private/sre/credentials.json",
  "humans": ["operator"],
  "stewards": ["knowledge-steward"],
  "single_valued_predicates": ["deployment-image"],
  "automatic_bindings": [],
  "database": {"kind": "postgres", "location": "SRE_DATABASE_DSN", "schema": "sre_suite"},
  "production_enabled": false
}
```

`location` is the name of a private DSN environment variable for PostgreSQL, or the path for SQLite. This service does not implicitly migrate PostgreSQL. Opaque tokens use the existing credential format, with exact scope/action grants and independent identities. Human membership is configured on the server; caller-supplied claims cannot grant it. Production scopes must first be registered by an authorized human using `environment-register`, including the expected preceding generation. Turning off `production_enabled` blocks production access. Production approval is human-only even when an automatic non-prod grant exists.

```sh
python -m sre_suite.control serve --config /private/sre/control.json \
  --certificate /private/sre/server.crt --key /private/sre/server.key --port 8443
python -m sre_suite.control request approve --request /private/sre/approve.json \
  --output /private/sre/approval-result
```

Control requests contain `scope`, `payload`, and `operation_id`. Their CLI writes the result privately. An approval payload contains `binding` and `expires_at`. A binding has exact `scope`, `context_version`, `artifact_digest`, `plan_digest`, `action`, and `target`; changing any field requires a different approval. Approvals expire within 24 hours and cannot be resurrected after revocation. Automatic non-prod authority is an explicit server-side `(subject,binding_sha256)` allowlist, not a general enable flag.

Set `SRE_ENGINEERING_ROLES_CONFIG` to a schema-2 JSON object with `roles` entries for `controller`, `implement`, `review`, `test`, and `qa`, each containing `subject` and `queue`. All five subjects and queues must differ. Set `SRE_ENGINEERING_ROLE` per worker. Only the controller may publish PRs or change Jira statuses. Give each worker its own evidence token; stage evidence includes the preceding artifact hashes. Controller/test processes do not require a Claude API key. Configure human approvers independently of all engineering and build subjects.

Use a private durable journal accessible by the workers that need the same execution artifacts. Multiple hosts require a shared POSIX filesystem with functioning advisory locks, atomic replacement and fsync, plus consistent ownership permissions. Local disks on unrelated hosts are **not** interchangeable. Temporal durability does not copy journal files. PostgreSQL persists shared knowledge, approvals, audit, outbox state and immutable evidence pointers; it is not a replacement for the execution journal. Back up both stores and Temporal history before claiming recovery readiness.

## Planning, epic and pilot handoff

Set `SRE_REQUIRE_AUTHENTICATED_APPROVALS=true` and the private `SRE_CONTROL_URL`, `SRE_CONTROL_TOKEN`, and optional loopback `SRE_CONTROL_CA_FILE`. Planning dry-run emits the exact authorization binding. Server approval is re-read before each Jira write and during handoff, in addition to the existing local approval digest.

An epic binding uses action `plan` and target keys `environment`, `url`, `project`, `repository`, `issue_type`, `summary`, `parent_issue`, `epic_name_field`. Supply the actual epic issue-type ID, an optional same-project initiative key, and an optional legacy Epic Name custom-field ID. Metadata discovery rejects unsupported required fields. The epic stores its planning identity as an issue property; it has no executable ticket contract.

After the epic receipt, set `SRE_JIRA_PARENT_KEY` to its key, review the newly bound child plan and approve its new digest. Child creation and subsequent verification require the exact parent epic. Publishing an epic, reconciling knowledge, or shipping a commit is a suite checkpoint: inspect the receipt and create a newly scoped/approved plan before continuing. This prevents silently changing an approved plan after Jira assigns identities or Git advances.

The existing `handoff` command produces the execution spec and read-only preflight. The suite can execute that command durably, but workflow start stays explicit. Dependencies are encoded in both the graph and Jira contracts. A ticket returning `ready_for_review` is not evidence that it has merged; dependency acceptance is rechecked by the existing Jira execution adapter.

## Deployment and image certification

An external trusted CI builder supplies an authenticated `image-build` evidence document. It must bind `source_repository`, baseline `source_revision`, actual `candidate_revision`, `source_artifact_digest` (the implementation evidence SHA), `plan_digest` (engineering manifest), `image_digest`, all four `stage_artifacts`, `chart_digest`, and `chart_revision`, with `schema_version=2` and `kind=image-build`. The builder token must differ from the four engineering identities. This adapter consumes that build attestation; it does not provide a container registry or replace the organization's isolated image builder.

Export the exact chart recipe with `python -m sre_suite.delivery export-chart --output PRIVATE_DIR`, review it in the GitOps repository, and pin its resulting commit. `prepare` runs the privately pinned Helm binary and compares its output to the exact expected resources. Argo CD must already be installed in the selected GKE cluster, with access to that pinned GitOps repository and project. This release manages namespace/application lifecycle in an existing cluster; it does not create cloud accounts or install cluster-wide operators.

The delivery request has `schema_version`, `binding`, `build`, `stages`, `verification`, and `expires_at`. The target in the binding carries the exact GCP project/location/cluster UID, target scope/context version, namespace/application/Argo project, GitOps URL/commit/path/chart digest, immutable image digest, candidate commit, replica count/port/health path, phase (`preview` or `release`), runtime environment, and optional expected preceding resource ownership/UID/spec digest. See the typed parser in `delivery/contracts.py` and the synthetic `tests/delivery_fixture.py` for the complete shape.

Private delivery worker configuration contains `request`, `journal`, `task_queue`, `builder`, `roles`, `verification_policy`, `kubernetes_url`, `kubernetes_ca`, `kubernetes_ca_sha256`, `helm_binary`, and `helm_sha256`. Tokens come from `SRE_GCP_ACCESS_TOKEN`, `SRE_KUBERNETES_TOKEN`, and the evidence/control variables. Explicit CA/binary hashes are rechecked. No ambient cloud-account discovery occurs. `runtime_environment` supports fixed configuration or Kubernetes Secret references; it cannot override the reserved service/scope/artifact attributes. Supply OTLP exporter endpoints and other service settings privately.

Preview delivery returns `ready_for_verification`, never final certification. Use a reviewed verification/fixture policy with `artifact_kind=container-image-v1` and the exact image digest. `service.version` records the candidate commit, while `sre.source.revision` records the original engineering source scope. Query mappings must preserve both meanings. Release/production promotion requires a passed certificate for that image and plan, a separately current target scope, and an active exact promotion approval. Source-only certificates cannot authorize image delivery. Readiness validates the actual pod image IDs and execution recipe again.

Shipping config contains `request`, four independent `roles`, and `github_url`. Its request contains `schema_version`, the merge `binding`, four stage evidence refs, and `expires_at`. Merge target fields are `environment`, `repository`, `git_origin`, `pull_request`, `head_sha`, `base_sha`, `base_branch`, `required_checks` (`name` + trusted GitHub `app_id`) and allowed independent `reviewers`. The exact tested head must be a single commit on the approved base. Branch protections may refuse the atomic fast-forward; the agent does not bypass them or synthesize an untested merge commit. A lost acknowledgement is reconciled against the remote ref/PR before another operation.

Rollback and cleanup are separate approved actions. Resource replacement requires the exact previous owner, namespace/application UIDs and application-spec digest, with resourceVersion compare-and-swap. Cleanup uses UID/resourceVersion delete preconditions and never deletes a newer generation. For expired previews, create a fresh maintenance binding but keep the historical cleanup target scope and ownership; cleanup does not reuse expired build evidence. Fresh verification/approval is required when rollback evidence has expired. A canceled or timed-out rollout stops and requires reconciliation; it does not automatically delete resources or invent rollback authority.

## Generic service journeys and analyses

Fixture policies may include `journeys` with `origins` and `routes`. Origins specify HTTPS `base_url`, a dedicated `SRE_JOURNEY_*_AUTH` variable and optional loopback `SRE_JOURNEY_CA_FILE`. Routes map each reviewed scenario path to fixed GET/POST steps (`origin`, `method`, `path`, `payload`, `success_status`). No response data is interpolated into future URLs, payloads or credentials. The runner injects the one reviewed trace ID and stable step idempotency key. POST intent is durably saved first; an uncertain POST is never blindly repeated. Responses are bounded, saved privately and evaluated by the separate fixture assessor. Service-specific fault/capability endpoints and their expected telemetry remain private configuration and must be acceptance-tested.

Analysis requests have `schema_version`, `role`, `scope`, `context_version`, `artifact_digest`, `plan_digest`, output `producer`, `inputs`, `parameters`, and `expires_at`. Each input names an authenticated evidence reference and its expected independent producer. Supported roles: `standards`, `topology`, `pipeline-health`, `assessment`, `regression`, `remediation`. Source-only standards use the exact snapshot digest; image standards additionally require independent build evidence and the candidate commit. Runtime analyses require the reviewed telemetry policy and exact candidate/plan labels. Regression requires two complete 120-check reports with matching policies and explicit baseline/candidate artifact identities. Turning a formerly passing check into not-applicable counts as regression. Remediation emits a proposed, evidence-linked Jira DAG; it never publishes work or starts implementation itself.

## Suite graph and operator acceptance

A suite plan contains a binding (`execute` or `remediate`), deadline, source repository/base branch, controller queue and topologically ordered nodes. Each node pins `id`, `kind`, dependencies, worker queue, private request SHA-256, up to two correction request hashes, and `checkpoint_after`. The binding target is `{environment,graph_sha256}`, where the digest covers nodes/source/queue/deadline. Private request envelopes contain `scope` and `payload`. Worker config has `plan`, a digest-to-file `requests` map, `journal`, `role` (`controller` or the exact local node ID), and `github_url`. `inspect` checks current authority without starting work. `start --authorize-start` starts one workflow ID with duplicate rejection. Native agent worker queues still need their own private policies and tokens.

Correction requests are distinct, pre-reviewed execution specs, not blind retries of a blocked model output. A correction requires the preceding authenticated failed-attempt receipt and remaining budget. Jira remediation work and its evidence must already be reviewed/published. After source advancement, regenerate scope/discovery/plan and approve it; do not edit an existing workflow's frozen graph. A finite graph can finish or pause for review. It is not an unbounded background autonomous loop.

Before live acceptance: verify private Jira fields/types/transitions, Claude CLI compatibility and budget, independent token grants, Git branch protections/CI app IDs, image-builder attestations, Kubernetes RBAC/Argo project policy, image non-root execution/health paths, OTLP routes, service journeys and all 120 private assertions. Exercise lost acknowledgements, revoked approval, changed generation, worker restart, PostgreSQL/Temporal recovery, unhealthy rollout and approved rollback. Perform production backup/restore/load/upgrade acceptance separately. No such live organization-specific acceptance is claimed by local fixtures.

Provider references: [Jira issue creation](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/), [Jira hierarchy levels](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-types/), [GKE cluster lookup](https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get), [Kubernetes concurrency control](https://kubernetes.io/docs/reference/using-api/api-concepts/), [delete preconditions](https://kubernetes.io/docs/reference/kubernetes-api/definitions/preconditions-v1-meta/), [Helm template](https://helm.sh/docs/helm/helm_template/).
