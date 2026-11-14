# Scoped Jira planning

Version 0.11 adds explicit project discovery, a reviewable plan dry-run, and opt-in issue publication. It implements a bounded first slice of P2-1/P2-2/P2-3. It does not generate a plan with a model, discover arbitrary projects, start engineering workflows, accept completed work, merge or deploy. Live Jira write acceptance remains pending private configuration.

## Private configuration and project checks

Use one authorized Jira Cloud project and repository. Export configuration privately; the CLI does not load `.env` automatically. The new planning flag is separate from the existing execution-report/transition flags.

| Variable | Meaning |
| --- | --- |
| `SRE_JIRA_BASE_URL`, `SRE_JIRA_EMAIL`, `SRE_JIRA_API_TOKEN` | Credential-free HTTPS origin and explicit Jira credentials; no account discovery |
| `SRE_JIRA_PROJECT`, `SRE_REPOSITORY` | Exact authorized project key and `owner/name` |
| `SRE_JIRA_ISSUE_TYPE_ID` | Explicit standalone issue type ID available in this project; subtasks are rejected |
| `SRE_JIRA_CONTRACT_FIELD` | Optional multiline text custom field on both create/edit screens, with no default value |
| `SRE_JIRA_PLANNING_WRITES_ENABLED` | Defaults to `false`; only `true` permits publishing a reviewed plan |
| `SRE_EVIDENCE_API_URL`, `SRE_EVIDENCE_API_TOKEN` | Existing authenticated context/evidence reads for the plan's exact scope |
| `SRE_EVIDENCE_CA_FILE` | Optional existing local pilot CA, restricted to loopback HTTPS as before |

Discovery needs only Jira/repository configuration; dry-run/publication also require evidence configuration. It checks the configured project, Browse/Create/Edit permissions, paginated issue-type and create-field metadata, and the contract field's multiline schema. A required field other than project, issue type or summary blocks this increment, even if Jira might supply a default. Custom field creation, workflow changes, assignments and arbitrary create-field overrides are not supported. Configure an approved project whose ordinary issue creation and editing automations do not execute or ship work automatically.

```sh
python -m sre_suite.jira_planning discover
```

The result contains bounded configuration metadata. Discovery sends only Jira GET requests. It is an observation of permissions and metadata, not proof of write-token scopes or post-function behavior.

## Plan contract

Start from [examples/jira-plan.json](../examples/jira-plan.json). Its evidence, timestamps, IDs and requirements are synthetic. Replace them privately with a reviewed plan and currently uploaded evidence; it will not authorize a live run unchanged.

The version 2 envelope contains `id`, `project`, `repository`, the full `scope`, and 1–20 `nodes`. Each node has an immutable `id`, a bounded `summary`, `depends_on` planned IDs, and `requirements`. Requirements are the existing execution ticket fields except `key`, `dependencies`, `scope` and `repository`; those identities are supplied by the plan and eventual Jira mapping. Requirements include baseline evidence, acceptance criteria, QA, telemetry, delivery and rollback. Each node's evidence must match the complete scope and be currently readable with a valid digest. Production and unscoped environments are rejected.

Duplicate IDs, self-dependencies, cycles, unresolved dependencies, identity overrides and missing criteria fail before publication. Dependencies are restricted to nodes in this plan. This increment supports one repository/revision/preview generation per plan; cross-repository plans and external prerequisites require a later design. A valid plan is a reviewed input, not an automatically inferred backlog.

The existing preview registration and evidence upload must happen first. Registration grants are still separate from planning/worker reads. The planner does not register previews, extend grants, upload baseline evidence or invent producers. For a local stack, supply the exact existing worker read token and CA privately; do not give the planner the controller token or database password. The local launcher does not automatically export planning credentials.

## Review without writing Jira

Use physical absolute paths. The new output directory must have an existing owner-only parent outside every Git checkout and without symlink components. The complete review contains private requirements and evidence references, so keep it out of public PRs.

```sh
python -m sre_suite.jira_planning dry-run /absolute/private/plan.json \
  --output /absolute/private/review-one
```

Read `review-one/review.json`. It contains the complete plan, dependency order, configured site/project/repository/issue type/field, context version, discovered capabilities and an `approval_digest`. The digest binds the plan and configured destination; credentials and the enable flag are excluded so secrets can rotate. Dry-run verifies every evidence record through the authenticated API and checks context consistency at its final read. It makes no Jira writes, model calls or workflow starts. Its output cannot overwrite an earlier review directory.

Approval is the operator's deliberate selection of that exact digest for this private CLI; it is not an independently authenticated human approval record or production release authorization. Evidence freshness and current scope are checked again during publication. Review does not reserve Jira metadata or the context version indefinitely.

## Explicit publication

After reviewing the plan and authorizing issue creation/editing, enable `SRE_JIRA_PLANNING_WRITES_ENABLED=true`. Create one durable, private journal directory outside Git. Every publisher for this plan/site/project must share that directory and reliable POSIX file locking. Use the same directory on retries.

```sh
mkdir -m 700 /absolute/private/planning-journal
python -m sre_suite.jira_planning publish /absolute/private/plan.json \
  --approve-digest REVIEWED_SHA256 \
  --journal /absolute/private/planning-journal
```

Publication repeats discovery and evidence checks before entering the journal lock. For each node in dependency order it:

1. Looks for the deterministic plan/node label. Multiple matches or incomplete search results block reconciliation.
2. Saves a durable creation intent **before** sending a single issue-creation POST. The request includes only project, issue type, summary, label and a plan fingerprint property.
3. Saves the acknowledged Jira key, or reconciles a previously created issue by exact label and fingerprint property. It verifies project, type, summary, labels and nonterminal status.
4. Maps planned dependency IDs to the actual preceding Jira keys. It creates the version 2 execution contract, verifies the optional field is still empty/editable, and writes it as plain-text Atlassian Document Format (ADF).
5. Rereads the field and identity before saving completion. At the end it checks every contract again and rechecks scope/evidence before returning `tickets_prepared` with the planned-ID-to-key mapping.

The contract field stays empty until the real issue key is known, so a partially created issue cannot be consumed as an executable ticket. A retry accepts an already matching contract; it does not overwrite changed content or restore a completed contract that someone removed. All Jira writes have full-scope checks immediately before and after them. A generation change during a remote request can still leave a created/edited issue; later writes and the success result are blocked. Jira and the evidence API do not share a transaction.

The existing execution reader accepts legacy string fields and plain ADF text paragraphs. Links, marks, mentions, code blocks and other rich nodes are rejected; it never follows content as instructions. The planner requires a multiline field because ordinary short text fields are unsuitable for full contracts. Plan execution still requires explicit runtime configuration, an execution spec using the **returned Jira key** and the same scope, preflight, and an authorized workflow start. Publication does not mark prerequisites accepted or dispatch dependent work. The runtime requires each prerequisite to reach a configured accepted status; a changed source revision needs a newly scoped/reviewed plan.

## Retry and recovery limits

Jira issue creation has no idempotency key in this adapter. If a POST response is lost, the journal retains its intent. A retry can adopt the matching remote issue after search visibility catches up, but **never sends another create POST for that uncertain node**. A crash after persisting intent but before sending the request also blocks on retry. Even an explicit create error stays uncertain: inspect the issue state and journal instead of deleting state or changing plan IDs to force a second write. This conservative behavior sacrifices automatic recovery when absence cannot be proved.

Search is permission-filtered and eventually consistent. An empty result is not proof that an earlier request failed. Keep the journal with its reliable locks/backups; using separate journals, losing state, changing labels, or concurrent third-party creation invalidates the duplicate-prevention boundary. The code refuses incomplete search pages and multiple matches. It does not recreate deleted issues or clean up partially published plans automatically.

A changed plan cannot reuse its stored plan ID. A changed scope, expired evidence, context version change during publication/retry, terminal status, edited marker, moved issue or changed contract blocks further work. Inspect already-created issues and explicitly review a new plan when appropriate. Fixed-value contract PUT retries converge on the same content after a lost response, but Jira automation can still run more than once if a response is uncertain before the field becomes readable. Concurrent human edits have no atomic compare-and-swap API here; use a dedicated synthetic project and reconcile conflicts rather than treating checks as a transaction.

The journal records delivery intent/results only. It is not a replacement for Temporal workflow history, a shared planning database or a distributed authorization service. Scheduled backlog generation, approval identity, a Temporal planning workflow, governed context reconciliation and live external acceptance remain future work.

## Verification and API references

Tests use the real local authenticated evidence API with synthetic Jira responses. They cover DAG validation, metadata pagination, permission/type/field constraints, scoped evidence, preview replacement before/during writes, concurrent publishers, lost create/edit responses, persisted intent across restart, pre-request crashes, uncertain invisible issues, edited markers/contracts, final verification, private review files, redaction and execution-reader compatibility. They do not create real Jira issues or invoke Claude.

API behavior follows Atlassian's [create issue and field metadata](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/), [enhanced JQL search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/) and [issue properties](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-properties/). Live permissions, screen configuration, search visibility, field-size limits and automation must still be checked with private account configuration.

## Hand off a published plan

Use the [guarded handoff command](planning-handoff.md) to verify the publication mapping and retained journal, generate a specification for the selected actual Jira key, and run preflight with private runtime configuration. Workflow start remains a separate explicit command.
