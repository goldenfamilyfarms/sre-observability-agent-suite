# Dynamic preview environments on GCP

Status: proposed implementation guidance, not provisioned infrastructure. This document adapts the supplied GitHub + Helm + Argo CD + GKE notes into a generic public design. It does not assert that any cluster, chart, repository connection or credential has been inspected. No real organization or service inventory is included.

## Target lifecycle

Use a shared, dedicated non-production GKE cluster with one namespace per preview as the initial candidate. Start with one service, one reusable Helm chart, isolated test data and complete create/update/delete coverage. A namespace is an organizational boundary; apply access and network controls before admitting workloads. Untrusted code that needs stronger isolation may require a separate cluster or project.

| Event | Intended behavior |
| --- | --- |
| PR opened without preview opt-in | Run ordinary CI; create no environment |
| Authorized maintainer requests a preview | Validate eligibility, budget and exact-release authorization; prepare isolated resources |
| Tests and image build succeed | Publish the immutable digest and approved deployment configuration |
| Argo reconciles the configuration | Deploy into the prepared namespace; wait for readiness, then smoke-test |
| A new PR commit arrives | Build and authorize the new release; reject stale build completions; reconcile the new digest |
| Label removed, PR closed/merged, authorization revoked or TTL expires | Withdraw desired state and clean up all owned resources |
| Event is missed or cleanup fails | Reconcile from current PR state and resource inventory; retry within bounds and escalate |

A `preview` label expresses intent, not authority to deploy arbitrary PR contents. A newer commit does not inherit an exact-release authorization. Untrusted fork PRs receive no privileged build/deployment credentials. A trusted dispatcher must validate the requesting actor and current PR head before writing desired state.

## Persistent foundation and ownership

Provision the shared foundation separately, for example through reviewed Terraform. Do not create a cluster per PR by default.

| Component | Owner and purpose |
| --- | --- |
| Non-production GCP project and GKE cluster | Platform team; approved capacity and separation from production |
| Artifact Registry | Build pipeline publishes images; GKE image-pull identity receives scoped read access |
| Gateway/Ingress, DNS and TLS | Platform team; restricted preview routes and certificates |
| Secret Manager and workload identities | Platform team; non-production secret access per workload/environment |
| Argo CD, ApplicationSet and fixed AppProject | Platform team; approved repositories, destinations and resource types |
| Namespace baseline | Platform controller; RBAC, admission, network policy, quotas and limits before application sync |
| Resource ledger, expiry and cleanup controller | Environment controller; ownership, external resources, budgets and deletion evidence |

Separate GitHub API access for PR discovery, Git read access for Argo's repo-server, CI image-publish access, GKE image-pull access and application access to Google APIs. A Pod workload identity is not the same permission path as pulling its image. Use short-lived CI credentials; use [Workload Identity Federation for GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity) for workload access without service-account key files. Operators that need the GKE API must have an approved network path to a private endpoint.

## Choose the ApplicationSet input

Argo's [pull request generator](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Generators-Pull-Request/) can filter GitHub PRs by label and expose `number` and `head_sha`; polling or an ApplicationSet webhook discovers changes. A direct pilot can map these to a namespace, chart revision and image tag. If used, build the exact PR head SHA, not GitHub's synthetic merge SHA. This design can race the image build and attempt deployment before an image exists. It also needs an independent authorization gate; the label filter alone is insufficient.

**Preferred suite design:** CI builds and tests the exact source revision, then the trusted shipping/environment controller checks authorization and publishes a configuration record to a protected deployment repository. Argo's [Git file generator](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Generators-Git/) reads those JSON/YAML records. Only successfully built, explicitly authorized digests become desired state. This is a project design choice, not a built-in CI/approval check performed by ApplicationSet.

Before publishing, atomically compare the current PR head, generation and authorization version. A late build for commit A must not overwrite commit B. Closing or expiring a preview must leave a tombstone/generation guard so a delayed build cannot recreate it. Removing a label or closing a PR does not automatically remove a Git-generator record: the environment controller must remove it, and the periodic reconciler must repair missed events.

Suggested private configuration record fields:

| Field | Meaning |
| --- | --- |
| `environmentId`, `generation`, `repositoryId`, `prNumber` | Stable, collision-resistant identity and optimistic concurrency |
| `service`, `sourceRevision`, `imageDigest`, `chartRevision` | Exact source/build and reviewed chart identity |
| `namespace`, `hostname`, `clusterRef` | Validated destination mapping; controller-owned allowlist |
| `releasePlanDigest`, `authorizationRef`, `gateEvidenceRefs` | Authorization and required quality evidence |
| `createdAt`, `expiresAt`, `owner`, `resourceRefs` | Cost control, accountability and cleanup inventory |

Use a DNS-safe environment name derived from repository identity and PR number, such as `demo-a1b2-pr-142`; never use an unchecked branch name. Record a generation when reusing the namespace across commits. Store references to credentials, never secret values, in the deployment record.

## Illustrative Git-generator wiring

This example is documentation only. Placeholder resources, the protected configuration writer, namespace controller and chart are **not** included in the repository. The chart must implement every parameter. Validate the installed Argo CD version, CRDs and project restrictions before applying an adapted manifest.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: demo-previews
  namespace: argocd
spec:
  goTemplate: true
  goTemplateOptions: ["missingkey=error"]
  generators:
    - git:
        repoURL: https://github.com/EXAMPLE_ORG/preview-config.git
        revision: main
        files:
          - path: previews/demo-service/*/config.json
  syncPolicy:
    preserveResourcesOnDeletion: false
  template:
    metadata:
      name: '{{.environmentId}}'
    spec:
      project: preview-services
      source:
        repoURL: https://github.com/EXAMPLE_ORG/preview-config.git
        targetRevision: '{{.chartRevision}}'
        path: charts/demo-service
        helm:
          releaseName: demo-service
          valueFiles: [values.yaml, values-preview.yaml]
          parameters:
            - name: image.digest
              value: '{{.imageDigest}}'
            - name: preview.environmentId
              value: '{{.environmentId}}'
            - name: preview.hostname
              value: '{{.hostname}}'
      destination:
        name: REGISTERED_NONPROD_CLUSTER
        namespace: '{{.namespace}}'
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

The environment controller must publish a record only after the namespace and its controls are ready. Namespace creation is intentionally owned outside this application. The protected record writer validates every substituted field; `missingkey=error` detects absent keys, not malicious or unauthorized values. Freeze `chartRevision` to a full reviewed commit. The mutable `main` branch is only the protected desired-state index.

Keep the AppProject fixed rather than templating it from PR content. Restrict source repositories, destination cluster/namespaces and allowed resource kinds; exclude Argo's control namespace and production destinations. Treat chart/hook changes as privileged inputs and enforce admission restrictions on workloads. See [Argo CD project controls](https://argo-cd.readthedocs.io/en/stable/user-guide/projects/). Automatic sync is appropriate only behind the implemented authorization boundary; this example does not supply that boundary.

## Helm, routing and state isolation

Make the chart accept an image repository and digest, hostname, environment ID, service account, dependency endpoints, resource requests/limits and replica count. Render the image as `repository@sha256:...`, rather than silently preferring an old tag. Use `.Release.Namespace` where an explicit namespace is needed. The same Helm release name can be used in separate namespaces. Keep preview defaults in `values-preview.yaml`; size JVM startup and steady-state requirements from measurements rather than copying arbitrary limits.

The hostname value does not create DNS, TLS or ingress. Verify actual routing, certificate coverage, authentication and namespace-specific backend selection. Prefer approved private access for preview URLs.

Apply namespace RBAC, ResourceQuota, LimitRange and workload admission controls before deployment. Use enforced default-deny ingress/egress policies, then permit only required DNS, routing and dependencies; test cross-preview denial. Verify the cluster's network-policy enforcement rather than assuming namespaces isolate traffic. See [GKE network policy](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/network-policy).

Use a separate database/schema and restricted user with synthetic or approved sanitized data. Separate Kafka topics or Pub/Sub topics/subscriptions when tests could affect shared consumers; consumer groups alone do not isolate produced messages. Use dedicated Redis resources where required; key prefixes are only a convention for trusted workloads. Mock or sandbox external integrations. Shared non-production dependencies need explicit authorization and safe write boundaries. Migrations must target only the preview's data store and have a cleanup/rollback strategy.

## Connect previews to the agent suite

The durable orchestrator owns preview lifecycle and approval state; Argo owns convergence of the approved Kubernetes desired state. A proposed lifecycle is Requested → Authorized → Preparing → Deploying → Ready → Testing → Expiring → Deleting → Deleted, with Failed/Blocked and bounded recovery paths. Argo Healthy alone does not prove smoke tests, telemetry correctness or final certification.

Planning records preview prerequisites and cleanup acceptance criteria in Jira. Shipping publishes the approved digest/configuration. QA and verification execute independently in the preview. Knowledge stores the environment resource graph and evidence; the controller appends lifecycle events. No existing agent gains permission to provision infrastructure from this document.

**Contract gap to close first:** current `Scope` contains only a service, environment class (`development`, `staging`, `production`) and revision. It cannot distinguish two preview instances at the same revision. Before preview execution, add an instance ID and generation to scope, releases, approvals, evidence, context keys and certification reports; migrate persisted records and reject mismatches. Map preview to a non-production class without losing its instance identity. Existing policy inputs must not be used as sufficient authorization for live previews until this binding is implemented.

Attach environment ID/generation, service, namespace, source revision and artifact identity to appropriate evidence and telemetry metadata. Filter dashboards and queries by the preview instance. Budget metric-label cardinality; avoid unbounded per-request identifiers. Preview alerts must not page production responders. Ticket checks remain targeted; final certification still accounts for all 120 slots and never transfers across environment instances or artifacts.

## Teardown, expiry and cost controls

Record every owned resource at creation with environment ID, generation, owner and expiry. Stop publishing new work, withdraw desired state, confirm workload deletion, then remove isolated databases/users, messaging resources, temporary IAM grants, routing records and the dedicated namespace in dependency-safe order. Never delete shared foundation resources by a name-prefix guess. Preserve redacted test evidence according to retention policy.

Argo [Application deletion](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Application-Deletion/) uses owner references and resource finalizers; `preserveResourcesOnDeletion` affects cascading cleanup. Verify effective controller deletion policy and finalizers in the pilot. `CreateNamespace=true` alone is not complete namespace ownership or external-resource cleanup. A stuck finalizer is a failure to investigate, not evidence that deletion succeeded.

Run a periodic TTL sweeper alongside PR event handling. Reconcile expired/closed previews against actual resources, retry cleanup idempotently, and report remaining resource IDs and costs after a bounded retry budget. Define maximum active previews, per-preview quotas, default/max TTL and an explicit extension policy. Those values require platform-owner decisions; none is enabled by this documentation.

## Pilot acceptance evidence

Before enabling automatic previews, demonstrate:

1. An unlabeled or unauthorized PR provisions nothing; untrusted forks receive no privileged credentials.
2. An authorized successful build creates one isolated preview with the correct digest, routing and synthetic data.
3. Failed builds and missing images publish no deployable configuration; readiness timeout blocks verification.
4. A second commit updates the correct preview; an older build or duplicate webhook cannot roll it back or recreate a closed preview.
5. Cross-preview network, data, secret and evidence access is denied; shared dependencies remain unaffected.
6. Smoke/telemetry results bind to the exact preview generation and artifact; they cannot satisfy another preview's gates.
7. Label removal, PR closure and TTL expiry each remove all owned resources, including after a missed event or partial cleanup failure.
8. Quotas, expiry extensions, bounded retries and rollback are exercised; the production destination is always rejected.

These are future integration acceptance tests. No GCP, Helm rendering, Argo reconciliation or teardown was executed by this documentation change.
