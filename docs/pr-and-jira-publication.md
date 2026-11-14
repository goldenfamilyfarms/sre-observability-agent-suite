# Automatic draft PRs and Jira transitions

Version 0.6.0 extends the [execution runtime](execution-runtime.md) with opt-in publication. A fully passed implementation/review/container-test/QA run creates a deterministic Git commit and feature branch, opens a **draft PR**, then moves the Jira issue to the configured review status. A normal failed gate moves the issue to the configured blocked status without publishing code. Preparation can move the issue to in-progress after its ticket, scope, context and evidence have been validated.

No merge, deployment, release approval, Jira Done/Accepted transition, automatic issue creation, branch overwrite, force push, PR reopening or cleanup deletion is implemented. The generated PR remains a human review task. Candidate evidence refers to the source revision and saved edits; it is not certification of the new commit or a deployed service.

## Configure private environment variables

The existing model, Jira, evidence and Temporal settings still apply. Add the following using the private copy of [.env.example](../.env.example):

| Variable | Meaning |
| --- | --- |
| `SRE_GITHUB_PRS_ENABLED` | `true` enables publication after all four gates pass; default `false`. |
| `SRE_GITHUB_TOKEN` | Explicit token for the configured `SRE_REPOSITORY`; Contents and Pull requests read/write permissions. Used by the control-plane adapter only, never sent to Claude or the test container. |
| `SRE_GITHUB_BASE_BRANCH` | Target branch, default `main`. Its current SHA must equal the ticket's full source revision. The reserved `sre/run-` prefix cannot be a base branch. |
| `SRE_JIRA_TRANSITIONS_ENABLED` | `true` enables the three configured milestones; default `false`. Requires both `SRE_JIRA_WRITES_ENABLED=true` and `SRE_GITHUB_PRS_ENABLED=true`. |
| `SRE_JIRA_TRANSITIONS_JSON` | Exact mappings for `in_progress`, `in_review`, and `blocked`. Each specifies `transition_id`, destination `status_id`, and permitted `from_status_ids`. IDs are strings, not names. |

The GitHub adapter supports `github.com` through `https://api.github.com`; enterprise hosts are not configured in this increment. GitHub credentials are supplied explicitly by your runtime environment, separate from the personal account connector used to author this repository.

An illustrative mapping is:

```json
{
  "in_progress": {"transition_id": "10", "status_id": "200", "from_status_ids": ["100"]},
  "in_review": {"transition_id": "20", "status_id": "300", "from_status_ids": ["200"]},
  "blocked": {"transition_id": "30", "status_id": "400", "from_status_ids": ["200"]}
}
```

These are **synthetic IDs**, not a default Jira workflow. Obtain your issue's current status ID and permitted transitions from Jira's issue/status and transitions APIs. Use `expand=transitions.fields` to inspect required fields. The adapter checks the actual transition ID, destination ID, source status and status category at execution time. It refuses unknown/terminal categories and transitions that require fields without defaults; this pilot does not supply resolution or arbitrary transition-screen fields. Supported nonterminal category keys are `new` and `indeterminate`.

The Jira account needs access to the selected project/issue, permission to transition issues and write issue properties. Select transitions and repository workflows whose automation is authorized for this pilot: creating a PR or changing Jira status can trigger independently configured automation. Use a synthetic repository and non-shipping Jira workflow for initial acceptance. Do not enable publication to a public repository for private source or infrastructure details. The adapter publishes only allowlisted file contents and generic hash-based PR metadata, but it cannot determine whether approved source itself contains confidential information.

Run `python -m sre_suite.execution.cli check-config` after exporting variables. It reports the four enable flags without printing tokens. The existing `worker`, `start`, `status`, `result`, and `cancel` commands are unchanged. A successful result now includes `pull_request` (URL, number, head SHA, branch, source revision and artifact checksum) and `jira_transitioned`. With PR publication disabled, patch-only execution still works and `pull_request` is null.

## Gates and publication sequence

1. Validate the exact service/environment/revision/**preview ID/generation**, Jira ticket and dependencies, canonical context, and evidence. Persist the manifest and perform the configured in-progress transition.
2. Execute the four existing gates. Stage evidence must attest the exact saved artifact and remain readable under the current scope grant.
3. Before publishing, revalidate those conditions. Read the configured GitHub base branch and source tree. The base SHA must still equal the tested source revision. Preserve regular-file executable modes, reject links/special files, and create a candidate from only the validated edits.
4. Create deterministic tree/commit objects and the branch `sre/run-<execution digest>`. The digest includes the complete scope and run ID. Fixed commit metadata makes retries produce the same candidate. Existing refs must already point to that exact candidate; no ref updates are performed.
5. Find an existing PR by the execution branch across all states, or create one draft PR. Validate its head/base repository, branch, SHA, title, body, draft/open state and identity. A closed, merged, retargeted or altered PR blocks reconciliation instead of being replaced.
6. Recheck the PR and full scope before moving Jira to review, writing the scoped report, and returning success. The private Jira report contains the PR reference; the public PR body contains hashes and generic gate information, not raw Jira descriptions or evidence bytes.

A normal failed gate uses the blocked transition and writes a blocked report. Authentication failures, expired/replaced previews, changed context, cancellation and infrastructure failures can stop execution before a safe Jira update is possible. In those cases Temporal reports failure/blocked/canceled state as appropriate, and Jira may remain in-progress. Never transition stale work merely to make the board look complete.

## Retry, concurrency and partial completion

The existing durable shared journal serializes publication attempts for the same execution through a file lock. All workers must use the same durable journal with reliable locking. Different intentional run IDs remain different executions; a configured Jira source-status rule helps prevent simultaneous runs from claiming the same issue. Jira is not a distributed lock service.

A Jira transition includes its deterministic `sre.transition.<digest>` property in the transition request. The property binds execution, manifest, scope digest, milestone and the configured source/destination IDs. After a lost response, retry checks both the marker and current status before accepting the transition. Conflicting markers, a destination reached without this marker, or a later human status change stop reconciliation. No comments or duplicate transitions are generated to recover uncertainty. This is not a claim of exactly-once behavior for Jira post-functions or third-party automation.

A lost Git object, branch or PR response is reconciled using deterministic objects and exact remote identities. Existing branches are never overwritten. A moved base requires a freshly scoped, tested execution; there is no automatic rebase. A draft that a human has marked ready, edited, closed, merged or retargeted requires operator reconciliation rather than an automatic replacement.

GitHub, Jira and the preview registry do not share a transaction. Full-scope guards run before each external write and again afterward, but a generation change can occur while a remote request is in flight. Tests explicitly cover a PR already created when the generation changes, and a Jira transition applied during such a change. The remaining writes and success report are blocked. Existing objects are retained for human inspection; the adapter does not close/delete/undo them. Inspect the execution branch/PR, transition property, Jira status and Temporal history before deciding how to proceed. A canceled request can also leave an acknowledged or uncertain remote write.

Stage timestamps and evidence TTLs still bound retries. Do not extend evidence timestamps or rotate a run ID merely to bypass a failed guard. GitHub/Jira tokens are excluded from policy fingerprints so credentials can rotate; publication flags, base branch and transition rules are included, so policy changes require a newly reviewed execution.

## Upgrade and validation

Drain or cancel/reconcile version 0.5 executions before upgrading workers. Version 0.6 adds publication policy to the manifest fingerprint and report fields; old manifests are not silently adopted. Use a new run ID after reviewing the current ticket and registering the appropriate scope. Database migrations, version 2 preview contracts, workflow activity names and the four-stage ordering are unchanged. Production approvals and exact-release non-prod promotion policy remain unchanged.

The fixture GitHub REST transport constructs actual Git blobs, trees and commits locally, allowing tests to check the candidate's contents and parent. Tests cover lost responses at every publication boundary, concurrent duplicate completion, blocked review, expired/replaced previews, transitions to terminal/unauthorized statuses, changed PR heads, conflicting markers and private-token isolation. The real Temporal integration test includes container QA, PR publication and Jira transitions with simulated lost responses, then replays the workflow history. GitHub/Jira HTTP responses are fixtures; no live Jira transition or paid Claude call is used in validation. A configured live synthetic pilot remains a separate acceptance step.

References: [GitHub Git commits](https://docs.github.com/en/rest/git/commits), [GitHub pull requests](https://docs.github.com/en/rest/pulls/pulls), and [Jira transition API and fields/properties](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-transitions-post).
