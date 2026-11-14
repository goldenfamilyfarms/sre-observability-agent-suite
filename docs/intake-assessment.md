# Intake, architecture assessment and planning

Version 0.24 adds callable read-only Intake/Architecture and Assessment agents, plus an explicit Temporal workflow that produces a reviewable Jira plan. The agents use deterministic source indicators, not a model. They operate on a bounded, operator-selected Git snapshot and independently authenticated evidence. They do not execute source, publish Jira issues, start engineering, merge or deploy.

## Scope and outputs

Intake reads **committed Git objects at a full 40-character revision**, not the worktree. Select 1–100 sorted, unique regular UTF-8 files, at most 64 KiB each and 256 KiB total. Paths, symlinks, binary files, missing objects, obvious credential filenames, submodules and excess budgets fail closed. Do not select files containing private configuration. Filename restrictions are not a general secret detector. Git hooks, replacement objects, global/system configuration, checkout filters and shell execution are disabled or unused. The selected source is never built or run.

`pin-source` computes a `sha256:` digest over the repository/revision and ordered file inventory (path, Git blob, SHA-256, mode and size). Place this digest in a private analysis request before starting. This **baseline source snapshot** is different from the engineering candidate digest and eventual deployed-image provenance. Discovery evidence cannot pass an engineering or certification gate by itself.

The manifest contains file hashes, indicator names, confidence labels and line references; it does not copy source text, endpoint strings or secret values. Its interaction graph links the service to observed HTTP server/client, database, Kafka, Pub/Sub and background-work categories. It does not resolve remote service names or prove a live call/event topology. The current rules recognize selected Spring/Python framework, OpenTelemetry, correlation, metrics, propagation and test markers. They are heuristics: comments, strings and dead code can match, and unsupported frameworks can be missed. Full AST analysis, multi-repository topology and runtime inventory remain outside this increment.

Assessment uses a different evidence subject and queue, without repository access. It validates the authenticated manifest, then emits five controls (traces, logs, metrics, propagation and tests), source references, a gap register, and an explicitly **unverified** maturity assessment. `static_coverage_score` is 20 points per observed control; it is not an operational maturity or certification score. Missing markers mean `not-observed`, never absence of a capability or N/A. Runtime verified controls remain zero and `certified` is always false. The planner recomputes the report rather than accepting a supplied score.

The proposed DAG contains architecture/coverage review, five instrumentation-validation tickets, discovered interaction tickets, then independent final certification. Spring findings select a generic standard Java-agent/Spring integration recommendation; runtime/version compatibility still requires review. Each ticket carries both evidence references and the existing requirements/rollback contract. The first ticket requires an operator to confirm inferred interactions and omitted coverage before implementation proceeds.

## Private preparation

Install `.[orchestration]` for the durable workflow. Direct role commands use the standard-library core. Prepare PostgreSQL/SQLite, the HTTPS evidence API and a registered preview using the existing [local pilot](local-pilot.md) and [authenticated API](authenticated-api.md) guides. Analysis does not register or extend a preview.

Provision **two distinct** API subjects and high-entropy tokens with `evidence:write`, `evidence:read` and `context:read` grants for exactly the same service, revision, non-production preview ID and generation. Supply only each worker's own token to its process. Keep the API/controller credentials, source checkout, request, source selection, journals and output private. The local launcher does not automatically create these two subjects or start discovery workers. Request expiry must be within one day and within the preview/token lifetime. Use the current context version.

Use physical absolute paths outside Git, with owner-only parent directories and files (0700/0600). Commands refuse symlink components, public permissions and output replacement. The source checkout itself is the one intentionally permitted Git directory. Create empty role journals with `mkdir -m 700`; retain them through retries and worker restarts. No `.env` file is loaded automatically.

Write a sorted JSON array such as `["app.py", "test_app.py"]` to a private `files.json`. Then:

```sh
python -m sre_suite.discovery pin-source \
  --repository-dir /absolute/private/service-checkout \
  --repository example/demo-service --revision FULL_COMMIT_SHA \
  --files /absolute/private/files.json --output /absolute/private/source-pin
```

Copy [the synthetic request example](../examples/discovery-request.json) privately and replace the repository, revision, selected files, returned digest, current scope/context, subjects, project, execution ID and expiry. The example cannot authorize a live run unchanged. `execution_id` becomes the proposed plan ID; changed inputs require a newly reviewed request. Source selection and the policy fingerprint are part of the immutable request identity.

## Direct role commands

In the intake process, export its own `SRE_EVIDENCE_API_URL`, `SRE_EVIDENCE_API_TOKEN` and optional loopback `SRE_EVIDENCE_CA_FILE`. Run:

```sh
python -m sre_suite.discovery intake --request /absolute/private/request.json \
  --repository-dir /absolute/private/service-checkout \
  --journal /absolute/private/intake-journal --output /absolute/private/intake-output
```

In a separate assessment process, export its different evidence token, then:

```sh
python -m sre_suite.discovery assessment --request /absolute/private/request.json \
  --intake-evidence /absolute/private/intake-output/evidence.json \
  --journal /absolute/private/assessment-journal --output /absolute/private/assessment-output
python -m sre_suite.discovery plan --request /absolute/private/request.json \
  --intake-evidence /absolute/private/intake-output/evidence.json \
  --assessment-evidence /absolute/private/assessment-output/evidence.json \
  --journal /absolute/private/assessment-journal --output /absolute/private/proposed-plan
```

Outputs are private evidence references or `plan.json` plus `review.json`. Reports are retrieved through the authenticated API. No raw source or tokens enter workflow inputs, evidence or Jira contracts. File paths and generic findings can still be sensitive; keep their API and Temporal namespace access restricted.

## Durable workflow

Configure the existing `SRE_TEMPORAL_ADDRESS`, `SRE_TEMPORAL_NAMESPACE`, TLS and optional API key privately. Run two worker processes with the same pinned request but separate role tokens, journals and queues:

```sh
# Intake identity; only this process receives the repository path.
python -m sre_suite.discovery worker --role intake \
  --request /absolute/private/request.json --journal /absolute/private/intake-journal \
  --repository-dir /absolute/private/service-checkout
# Assessment identity, in another process.
python -m sre_suite.discovery worker --role assessment \
  --request /absolute/private/request.json --journal /absolute/private/assessment-journal
```

Start deliberately from a configured operator process with scoped evidence reads:

```sh
python -m sre_suite.discovery start --request /absolute/private/request.json --authorize-analysis
```

`ArchitectureAssessmentPlanning` runs intake → independent assessment → proposed plan. It never dispatches engineering or Jira publication. The workflow ID is derived from the entire request and duplicate starts are rejected. Workers reload the private authorization before each activity and reject changed requests, policies, roles and receipts. Each child receives only evidence configuration, CA settings and PATH/LANG, not Jira, Claude or Temporal credentials. Source reads have a 120-second aggregate budget, subprocesses a 300-second budget, activities at most three retries within 12 minutes, and the workflow a 40-minute deadline. Heartbeats and process-group termination support cancellation.

Export a completed plan while its evidence and scope remain current:

```sh
python -m sre_suite.discovery result --request /absolute/private/request.json \
  --journal /absolute/private/assessment-journal --output /absolute/private/proposed-plan
```

Export re-reads authenticated evidence, checks current scope/context and regenerates the plan before writing. A completed history cannot resurrect an expired or replaced preview. A blocked workflow is not a plan. Keep one reliable, shared POSIX journal per role/request for every eligible worker, or pin the queues to the hosts holding those journals. Separate/lost journals do not provide cross-host exactly-once recovery. Temporal owns canonical workflow state; local journals only preserve immutable publication bytes and observation timestamps. Retries after an upload acknowledgement is lost reuse those bytes and evidence IDs. Policy upgrades require fresh requests/evidence; they cannot silently reuse an old plan.

## Reviewed planning and explicit pilot handoff

In the planner's private environment configure the expected `SRE_INTAKE_SUBJECT` and `SRE_ASSESSMENT_SUBJECT` together, plus the existing Jira/repository/evidence configuration. They must match the request and each other must differ. They are approval-target inputs, not bearer tokens. The optional `discovery` field in `Plan` binds the request, selected source digest, context version, rule-policy digest and both authenticated evidence references. Planning dry-run, publication/retry and handoff revalidate the lineage and exact generated DAG. Edited objectives, dependencies or references are rejected. Revoked/expired scope, wrong artifact/revision/preview ID/generation or a changed context version blocks further progress.

```sh
python -m sre_suite.jira_planning dry-run /absolute/private/proposed-plan/plan.json \
  --output /absolute/private/jira-review
```

Review source coverage, findings, gaps and every ticket. Follow [Jira publication](jira-planning.md) only after approving the returned digest, then [planning-to-pilot handoff](planning-handoff.md) to generate the execution spec and run preflight. Workflow start remains explicit; discovery is not production or non-production promotion authorization. Existing engineering candidate/plan checks and final 120-check certification remain in force.

Manual plans remain supported and serialize identically when `discovery` is absent. Removing provenance or changing planner identities changes the approval digest and cannot reuse approval of the assessed plan. A materially different manually authored plan needs its own review/approval and does not claim assessed-plan lineage. This increment does not add an authenticated human approval service or dispatch the entire dependency graph automatically. Source-changing tickets require fresh revision-scoped evidence and replanning for subsequent work; the current plan is one pinned baseline, not authority to reuse stale evidence after a change.

## Validation and limits

Unit tests use committed synthetic repositories and the real local evidence API to cover source pinning, secret-text non-disclosure, source bounds, evidence/producer substitutions, forged assessment scores, stale scopes, exact-plan approval, Jira retry behavior and unchanged legacy plans. Orchestration tests use real Temporal and HTTPS, installed subprocess workers, independent tokens, lost-ack recovery, replay, generation revocation, private result export and cancellation. No live Jira, Claude, employer repository, cloud or deployment calls are part of these tests. Live private-service discovery and the resulting engineering tickets still need operator acceptance.
