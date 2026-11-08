# ADR-011: gated draft PR publication and bounded Jira transitions

Status: accepted for the execution pilot; live-account acceptance pending.

## Decision

Add explicit environment-controlled GitHub draft-PR publication and Jira `in_progress`, `in_review` and `blocked` transitions. The dispatcher owns permission. Model output cannot choose a repository, branch, Jira transition or credential. Publish only after implementation, review, configured container tests and QA all pass; preserve preview ID/generation and source revision in execution identity and evidence checks.

Use deterministic Git objects and execution branches, exact remote reconciliation, and a shared-journal publication lock. Never update an existing branch, reopen a PR, merge, deploy or transition Jira to a terminal category. Include a transition marker in Jira's transition request, then require both marker and destination status to match on retry. Jira review requires the draft PR still to reference the tested candidate.

## Consequences and evidence

Unit tests use real local Git objects behind fixture HTTP transports. A real Temporal/container test now covers the complete publication sequence, lost PR/transition/report responses and history replay. Neither real Jira transitions nor paid Claude completions have been exercised.

Cross-service writes are not atomic. If a preview changes during a remote write, retain the partial artifact, block subsequent writes/reporting and require human reconciliation. Do not attempt destructive rollback. GitHub Actions and Jira workflow automation remain outside this adapter's control; initial live acceptance must use approved non-shipping automation.

Manifest policy adds publication settings. Drain/reconcile old-version executions before upgrading; do not adopt old journals under new publication authority. The runtime produces review artifacts only. Production approval signals, shipping, live telemetry certification and automatic Jira planning/issue creation remain later work. See [configuration and failure behavior](../pr-and-jira-publication.md).
