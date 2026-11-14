# Temporal, Claude Code and Jira execution pilot

Package 0.9.0 implements an opt-in engineering execution path:

```text
Jira JSON ticket + exact preview scope
  -> validated context and evidence + exported Git commit
  -> Claude implementation -> independent Claude review
  -> isolated test container -> independent Claude QA
  -> evidence-backed patch, optional draft PR and Jira status/result writeback
  -> human review
```

Temporal owns durable progress, retries and cancellation. Each model role uses a fresh Claude Code CLI session. The adapter makes real CLI/API calls when enabled; automated tests substitute a CLI executable and Jira HTTP transport. A successful run creates a patch artifact and, when explicitly enabled, a commit on a new execution branch and draft PR. Follow the [publication guide](pr-and-jira-publication.md) for GitHub/Jira transition settings. No merge, deployment or service certificate is produced. The implementation/review/QA sessions share one configured service principal; separate authenticated reviewer identities remain future work.

## Private configuration

Install Python 3.11+, Git, Docker, a compatible Claude Code CLI, and the optional runtime dependencies:

```sh
python -m pip install ".[postgres,orchestration]"
cp .env.example .env
```

Fill in the private copy of [.env.example](../.env.example). The app does not load it automatically. Export the variables through your process manager or, after reviewing your own file, in your shell:

```sh
set -a
. ./.env
set +a
python -m sre_suite.execution.cli check-config
```

`check-config` validates configuration without contacting external services. It prints only validity and enable flags, including GitHub PR and Jira transition flags. Both Claude invocation and Jira writeback default to disabled; set `SRE_CLAUDE_ENABLED=true` to permit model calls and `SRE_JIRA_WRITES_ENABLED=true` to permit the execution-property write. Jira reads and authenticated evidence reads/uploads are part of an enabled execution even when Jira writeback is disabled.

| Variables | Purpose |
| --- | --- |
| `SRE_JIRA_BASE_URL`, `SRE_JIRA_EMAIL`, `SRE_JIRA_API_TOKEN` | Jira Cloud HTTPS base URL and Basic email/API-token authentication. URLs must not contain credentials. |
| `SRE_JIRA_PROJECT`, `SRE_JIRA_CONTRACT_FIELD` | One permitted project and a `customfield_N` containing the version 2 ticket envelope as a legacy string or plain-text ADF paragraphs. The planning publisher requires a multiline text field. |
| `SRE_JIRA_DONE_STATUSES` | Comma-separated exact status names accepted for prerequisites; no surrounding spaces. |
| `SRE_EVIDENCE_API_URL`, `SRE_EVIDENCE_API_TOKEN` | Existing HTTPS evidence/context API and exact-scope service grant with upload, read and context permissions. |
| `SRE_REPOSITORY`, `SRE_REPO_DIR` | Approved `owner/name` and local checkout containing the ticket's full 40-character commit SHA. |
| `SRE_WORK_ROOT` | Durable private artifact journal outside the checkout, permission 0700. All workers for the queue must share it and support reliable file locking/atomic rename. |
| `SRE_ALLOWED_FILES_JSON` | Explicit relative paths the model may read and change; no wildcards, traversal, `.git`, `.github`, `.claude` or `.env` files. |
| `SRE_TEST_IMAGE`, `SRE_TEST_COMMAND_JSON` | Approved, pre-pulled image pinned by SHA-256 digest; bounded argv array, no host shell. Image must provide `python` and test dependencies. |
| `SRE_CLAUDE_BINARY`, `SRE_CLAUDE_MODEL`, `ANTHROPIC_API_KEY` | Explicit CLI executable, approved model ID and private API key. The CLI flags were inspected with version 2.1.319. |
| `SRE_WORKER_TIMEOUT_SECONDS`, `SRE_CLAUDE_BUDGET_USD` | 10–1800 seconds per worker; positive estimated model budget up to $20 per invocation, default $2. |
| `SRE_TEMPORAL_ADDRESS`, `SRE_TEMPORAL_NAMESPACE`, `SRE_TEMPORAL_TASK_QUEUE` | Existing Temporal service and isolated queue. Defaults are loopback:7233, `default`, `sre-engineering-v1`. |
| `SRE_TEMPORAL_TLS`, `SRE_TEMPORAL_API_KEY` | TLS is required for non-loopback endpoints; API key is optional according to service configuration. |

The Temporal namespace and queue must be restricted to authorized operators/workers. Workflow input is not itself a new grant: the worker also requires the configured project/repository/file set, exact ticket scope, and evidence-service authorization. This pilot does not provision a Temporal server, credentials or an identity provider.

## Prepare a ticket and preview

1. Configure the [evidence service](authenticated-api.md) and [PostgreSQL persistence](postgresql.md) if using PostgreSQL. A trusted operator must register the preview instance/generation and grant access to its full scope. The execution worker cannot create previews or issue service grants.
2. Upload actual baseline evidence through that service, then use its returned metadata in the ticket. Scope includes service, environment, revision, **environment ID and generation**. The synthetic evidence in the example is not usable authorization.
3. Create or select an authorized Jira issue. Put the JSON text from [the ticket example](../examples/jira-ticket.json), adjusted to your issue, repository, scope, criteria and evidence, into the configured text field. The worker reads this field through Jira REST v3; it does not infer executable instructions from a free-form description. Dependencies must be issue keys in the same permitted project, already in accepted statuses.
4. Adjust [the start specification](../examples/execution.json) to match the ticket's scope and key. Choose a unique `run_id` for this reviewed attempt. Change the run ID when intentionally restarting with changed inputs. Do not use a new ID to bypass stale evidence or a failed gate.
5. Pre-pull the approved test image and verify that your configured test command runs meaningful assertions. The example image supports standard-library Python tests. Dependencies cannot be downloaded inside the test container.

The fixture examples use expired synthetic timestamps and a placeholder revision deliberately. Replace them with fresh verified evidence; do not simply extend timestamps on old evidence. A changed generation, expired preview/evidence, altered ticket, unmet dependency, changed execution policy or changed context blocks subsequent work. Context changes require a newly reviewed execution.

## Preflight

After preparing the private environment, source, journal and worker, run `python -m sre_suite.execution.cli preflight examples/execution.json`. It performs read-only checks for this exact scope and reports capabilities that still need a live pilot. Exit 1 means blocked; exit 2 means read checks passed with unverified capabilities. It does not start work or grant permission. See the [preflight guide](preflight.md).

## Start, inspect and retrieve the patch

With the environment exported and the external services available, start the worker in one terminal:

```sh
unset SRE_PILOT_SETUP_TOKEN  # Controller authority is only for setup.
python -m sre_suite.execution.cli worker
```

Start an execution from another configured terminal:

```sh
python -m sre_suite.execution.cli start examples/execution.json
python -m sre_suite.execution.cli status WORKFLOW_ID
python -m sre_suite.execution.cli result WORKFLOW_ID
```

`start` prints the deterministic workflow ID. `status` reports the Temporal lifecycle state; `result` waits for the final `ready_for_review` or `blocked` report. Use the returned `patch_artifact` checksum to export a successful implementation artifact on a host with access to the private journal:

```sh
python -m sre_suite.execution.cli export-patch WORKFLOW_ID --digest PATCH_ARTIFACT --output /absolute/path/to/review.patch
```

The destination must not exist. Exporting an implementation patch does not attest that all later gates passed: inspect the final outcome and evidence before review. The checkout is never modified by the worker. To cancel:

```sh
python -m sre_suite.execution.cli cancel WORKFLOW_ID
```

When enabled, Jira writeback stores a deterministic JSON report at the issue property `sre.run.<SHA256 of execution ID>`. It includes scope, outcome, stage evidence references and patch checksum, with `merged=false` and `deployed=false`. Existing identical content is accepted on retry; conflicting content is rejected. No issue comments or new issues are generated. Optional status transitions are described in the [publication guide](pr-and-jira-publication.md). Read the property through Jira's REST API or a separately configured UI integration. Issue properties inherit Jira access visibility; keep raw source, test output and evidence bytes in the private evidence service/journal, not the property.

## Boundaries, recovery and limits

- Claude runs from an empty temporary directory with bare/restricted mode, no built-in tools, no MCP servers, no repository configuration, no slash commands and no session persistence. It receives selected source and context through stdin and returns validated replacement-file contents. Review/QA cannot propose edits. Jira/evidence/GitHub tokens are not forwarded to the CLI. Selected source/context is sent to the configured model provider: supply only approved content.
- Tests use a fresh commit export with proposed edits in a Docker container. The filesystem and source mount are read-only, networking is disabled, the process runs as a non-root user, capabilities are dropped and CPU/memory/PID/time/output limits apply. Runtime credentials and the Docker socket are not mounted. The image must be approved; image-baked configuration and dependencies are operator-controlled. Docker is a pilot isolation boundary, not a proven defense against arbitrary hostile code or kernel exploits.
- The source export is limited to 2,000 entries/20 MB, rejects symlinks/special files, and excludes protected configuration. Model input is bounded to 500 KB; edits total at most 250 KB. This is a small-repository pilot.
- Temporal activities retry transient HTTP failures at most three times; validation, authorization and model-output failures stop the run. Workflow timeout is four hours; activity heartbeat timeout is 45 seconds, throttled to one second. Subprocess loops heartbeat frequently and kill their process groups on cancellation. Blocking HTTP calls have a 20-second socket timeout; container cleanup may add 20 seconds. A container-local watchdog also stops tests if the host worker dies.
- The journal saves model output atomically before evidence upload, so a lost activity acknowledgement can reuse the same bytes/timestamps. Evidence uploads and Jira property writes reconcile retries. Back up the journal and evidence store alongside Temporal history. Reliable shared filesystem locking is required; this is not an object-store or distributed journal implementation.
- A crash after a model API call but before the result is saved can incur another charge. Claude's `--max-budget-usd` is an estimate and may overshoot; it is not a strict billing ceiling. External account spending controls remain necessary for a hard operational budget.
- Each gate rechecks the full preview scope, Jira contract and canonical context. Evidence is bound to the saved artifact digest. Jira writeback is not transactional with a concurrent preview change; reports are scoped historical records and never grant promotion permission. Infrastructure failures can leave the workflow blocked without Jira writeback; inspect Temporal status and private artifacts.
- Separate Claude sessions provide independent assessments, not separately authenticated people. There is no shipping adapter, production approval signal, production execution, 120-check telemetry executor, or automatic non-production promotion in this increment. Existing explicit non-prod authorization and human production-approval policy remain unchanged.

## Validation

The default suite runs offline. The separate engine suite requires the optional SDK, Docker, the pinned test image, and network access to download Temporal's temporary local test-server binary on first use:

```sh
python -m unittest discover -v
docker pull python@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
python -m unittest discover -s orchestration_tests -v
```

Tests run a real local Temporal engine, replay its history, restart a worker after a saved result loses acknowledgement, cancel work, exhaust retries, reject duplicate workflow IDs, reconcile a lost Jira-write response, and exercise constrained Docker execution. Jira responses and model completions are fixtures; no live Jira issue or paid Claude completion was used for validation. A configured synthetic live pilot is still required before claiming operational readiness.

API references: [Temporal Python SDK](https://github.com/temporalio/sdk-python), [Temporal test environment](https://python.temporal.io/temporalio.testing.WorkflowEnvironment.html), [Claude Code CLI](https://code.claude.com/docs/en/cli-reference), [Jira issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/), and [Jira issue properties](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-properties/).

### Prepare the preview and baseline

Use [pilot-setup](pilot-setup.md) to register an explicitly authorized generation, upload a baseline and generate `execution.json`. Supply the returned evidence metadata in the Jira execution contract before preflight. The command needs only the evidence service URL and separate controller/worker credentials; it does not require Temporal, Jira or model credentials and never starts a workflow.

### Local service startup

The [local pilot package](local-pilot.md) supplies PostgreSQL, migrations, a persistent Temporal development server, local HTTPS evidence hosting and optional worker startup. It keeps workflow execution separate and preserves existing preview-generation guards.

For optional candidate-bound Prometheus/Loki/Tempo observations, follow the [engineering telemetry guide](engineering-telemetry.md). This requires a separate telemetry worker and independently authorized preview setup; it does not grant deployment permission.
