# Read-only preflight and readiness report

Version 0.7 adds a diagnostic command for a **specific version 2 execution specification**. It checks the environment you intend to use before starting work. It does not start a workflow, run a model completion, execute tests, pull images, upload evidence, create a PR, transition Jira, merge or deploy.

## Run it

Use the private environment configuration from [.env.example](../.env.example) and the [runtime guide](execution-runtime.md). Install the orchestration extra, prepare your existing journal directory with owner-only permissions, pre-pull the approved test image, and start the intended Temporal worker separately. Then run:

```sh
python -m sre_suite.execution.cli preflight examples/execution.json
```

Replace the example's synthetic scope/revision/timestamps with a reviewed ticket and fresh evidence first. The command does not load `.env` automatically. It uses the configured credentials for read-only requests even when publication flags are disabled. `check-config` remains the offline configuration-only alternative.

Preflight is intended for a fresh execution. A retained workflow ID or an existing journal manifest blocks a fresh-start recommendation. Inspect existing work with `status`/`result` and reconcile it; do not change run IDs to bypass failed guards. A preflight result does not resume or repair a previous run.

## Output and exit codes

The command writes one JSON report to standard output. It contains a report schema version, UTC check time, execution digest, overall status, `read_only=true`, and named checks. Each check has `pass`, `fail`, `unverified`, or `skipped` status, a static explanation and an optional redacted error category. Credentials, private account IDs, raw HTTP/process responses, local paths, Jira text and evidence contents are not printed.

| Exit code | Overall status | Meaning |
| --- | --- | --- |
| `1` | `blocked` | At least one check failed. Correct the reported prerequisite before attempting a run. |
| `2` | `needs_validation` | Read checks passed, but one or more capabilities require a separately authorized live test. This is the expected successful read-only result in the current pilot. |
| `0` | `passed` | No failed or unverified checks. The current pilot deliberately retains unverified execution/write capabilities, so it normally does not produce this result. |

A failed check takes precedence over unverified capabilities. Disabled optional publication features are skipped, not treated as permission failures. Disabled Claude execution is a failure because the configured ticket cannot execute. No flag is enabled or changed by this command.

Treat the report as an observation, **not an execution grant, certificate or shipping approval**. Do not use exit code 2 as an automatic green light. Inspect its unverified items, then perform the separately authorized synthetic pilot. The runtime still applies all of its own guards; it never accepts a preflight report as a substitute.

## What is checked

| Area | Read-only checks | What remains unverified |
| --- | --- | --- |
| Configuration and scope | Required variables, strict enable flags, full source revision, non-production environment, preview ID/generation and run ID; bounded specification file | Future changes to credentials or policy |
| Local source | Export the exact commit using the existing path/file/size restrictions; inspect only the configured source set | Whether the proposed future code passes tests |
| Journal | Existing owner-only writable directory outside the checkout; no saved manifest for this execution ID | Shared locking semantics, durability, backups and actual writes |
| Claude | Run the trusted configured executable's help path without credentials and check required flags; authenticate a GET request for the configured model's metadata | Paid inference permission, quota, billing and a successful structured CLI completion |
| Docker | Inspect the exact local image digest through the daemon | Running the test command, image dependencies and effective container isolation |
| Jira | Active authenticated account; ticket/repository/scope/dependencies; issue-level Browse/Edit/Transition permissions as applicable | Token write scopes, post-functions and third-party automation |
| Jira transitions | Destination IDs/category are nonterminal; milestone source IDs connect; the initial in-progress transition is available without missing required fields | Availability of review/blocked transitions from future issue statuses |
| GitHub | Configured repository identity and accessibility, archive/disabled state, reported push role if available, exact base-branch revision | Token Contents/PR write scopes, branch rules and successful publication; missing role metadata is explicitly unverified |
| Evidence/context | Authenticated context and digest-verified baseline evidence reads for the exact current preview generation | Evidence-upload permission; no write probe is sent |
| Temporal | TLS/address/namespace/queue configuration; registered accessible namespace; workflow and activity pollers observed in the last five minutes; absence of a retained execution ID | Workflow-start permission, current worker capacity and future availability |

The Claude metadata probe uses `GET /v1/models/{model_id}` with API version `2026-10-21` and the configured API key. Use a model ID or alias supported by that API, not a CLI-only shorthand. The current runtime assumes an explicitly configured key usable with the direct Anthropic API; an organization-wide credential requiring extra workspace selection or a third-party model gateway is outside this pilot. Retired/unknown model lifecycle states fail the check when returned. Metadata access never counts as a successful inference test.

The Temporal probe uses namespace/task-queue/workflow **describe** operations. It does not poll tasks, register namespaces, start or cancel workflows, or invoke activities. A recently recorded poller can have stopped already, and workflow retention can remove old IDs; the journal check provides an additional local observation. Neither observation is a reservation.

At the end, the command rereads the ticket, canonical context/version and baseline evidence under the full scope. A preview replacement, expiry or ticket/context change during inspection blocks the report. The final fence is still a point-in-time observation: the environment may change immediately afterward, so normal runtime generation checks remain necessary.

## Read-only boundaries and limits

The HTTP wrapper permits GET requests and only the existing read-semantic `POST /v1/context` and `POST /v1/evidence/read` endpoints. Uploads, transitions, issue properties, Git objects, refs, PR creation and model message endpoints cannot be called through it. The existing scoped API continues to authenticate those read requests normally; no new API route, grant or migration is added.

The source checkout and durable journal are not modified. Source export and CLI help inspection use automatically cleaned temporary directories. Local Git, Docker and the configured Claude executable must be trusted installations. The CLI help process receives no model/Jira/GitHub/evidence credentials. The command never creates/chmods the journal or installs missing dependencies.

HTTP calls have a five-second socket timeout and bounded response size. Local help/image inspection has ten-second process limits. A 90-second check budget is checked between operations and in subprocess loops; it is not a hard end-to-end wall-clock guarantee for DNS or a slowly streaming HTTP response. Temporal connection and describe operations have separate bounded waits. Errors are reported with static categories, and dependent ticket/evidence checks are skipped when identity or scope validation fails.

Jira/GitHub permission responses describe what can be observed through their read APIs. They do not prove write-token scopes or the behavior of external automation. Preflight deliberately does not create temporary issues/PRs/evidence or transition out and back to test permissions. Live acceptance must use an approved synthetic issue/repository and non-shipping automation.

## Verification

Default tests use fixture Jira/GitHub/model responses and the real local scoped evidence API. They verify generation/expiry and final-fence rejection, redaction, strict read-only transport behavior, scope mismatch, denied permissions, unavailable transitions, moved branches, existing journals, missing images/CLI flags, exit codes and deadline handling. Tests also confirm that read-only checks leave remote fixture state and journal contents unchanged.

Real Temporal integration tests cover observed workflow/activity pollers, absent workers, a missing namespace, an existing execution ID and zero workflows created by preflight. The standard publication/container and PostgreSQL suites remain regression checks. Real external credential permissions and paid model completion have not been tested with user credentials.

References: [Claude model metadata](https://platform.claude.com/docs/en/api/models/retrieve), [Jira issue permissions](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get), [GitHub repository metadata](https://docs.github.com/en/rest/repos/repos#get-a-repository), and [Temporal Python SDK](https://github.com/temporalio/sdk-python).
