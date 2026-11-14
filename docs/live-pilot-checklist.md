# One-ticket live pilot checklist

Version 0.10 prepares a synthetic repository, a version 2 Jira execution contract and private inputs for the existing local stack. The live acceptance target is **one real Claude Code implementation, review and QA run ending in a tested draft PR and a nonterminal Jira review status**. Preparing these inputs does not execute that run. Credentials are supplied privately by the operator.

## Reviewed demo and boundaries

| Input | Reviewed value |
| --- | --- |
| Demo repository | [goldenfamilyfarms/sre-observability-pilot-demo](https://github.com/goldenfamilyfarms/sre-observability-pilot-demo) |
| Repository ID | `1450500737` |
| Bootstrap PR / preview anchor | [PR #1](https://github.com/goldenfamilyfarms/sre-observability-pilot-demo/pull/1) |
| Current publication base (bootstrap PR merged) | `main` |
| Reviewed baseline commit | `a492698b84cc4cda04d5e64d3f638a460827e129` |
| Preview identity / initial generation | `preview-r1450500737-pr1` / `1` |
| Service / environment | `pilot-log-demo` / `development` |
| Worker edit allowlist | `app.py` only |
| Candidate test command | `["python", "acceptance.py"]` |

Use the current reviewed suite feature branch, including the Phase 1 foundation. The demo bootstrap PR was merged before this handoff increment; use its current main baseline and recheck the full SHA. The bootstrap PR number is only the illustrative metadata anchor; explicitly authorize the selected preview identity/generation before setup. The worker creates a different draft PR targeting main. No additional merge is needed. Preview registration here is metadata, not a cloud environment.

The demo is a pure request-handler function, without a listener, dependencies or production data. It deliberately lacks `trace_id` in its structured logs. The ticket requires exactly 32 lowercase hexadecimal characters, excluding the all-zero value; invalid context must be omitted. Response behavior and sanitized log fields must remain unchanged. This is synthetic correlation, not a W3C header parser, OpenTelemetry integration or service certification.

The demo's three regression tests pass initially. Its seven-test strict acceptance gate initially fails exactly `test_valid_context_is_correlated`; after implementation all seven must pass without skips. `--baseline-report` recognizes that exact initial gap and is **never** an authorized candidate test command. Demo CI permits the known gap on the bootstrap branch and `main`, and requires strict acceptance on execution branches. The runtime always uses the strict gate. Tests and CI stay outside the worker's edit allowlist. As with any same-process Python test harness, this constrains routine changes but is not a defense against deliberately hostile code that tampers with its interpreter; review the generated `app.py` before accepting it.

## 1. Prepare private prerequisites

- [ ] Review the demo and suite PRs. Check that the demo base still points to the reviewed full SHA above. Do not move it during execution.
- [ ] Install the suite with `python -m pip install '.[postgres,orchestration]'` from the reviewed branch in a virtual environment. Install trusted Git, Docker, OpenSSL, Temporal CLI and Claude Code. See [local startup](local-pilot.md) for host requirements.
- [ ] Pre-pull both approved images. Startup and test execution do not pull images:

```sh
docker pull postgres@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3
docker pull python@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
```

- [ ] Create/select one synthetic issue in your authorized Jira project. Record its real key, contract custom field for plain JSON text, current status and permitted nonterminal in-progress/review/blocked transition IDs. This preparation does not create an issue. The [publication guide](pr-and-jira-publication.md) explains permissions and transition mappings.
- [ ] Confirm that the selected Jira workflow and repository automations cannot ship or deploy this pilot. Establish an explicit model budget for the complete run. The generated USD 2 setting bounds each CLI invocation, not total spending across stages and retries.
- [ ] Choose a new private parent directory outside **every** Git checkout, with physical absolute paths (no symlink components) and mode 0700. Keep credentials, stack state, evidence and results there. The following `/absolute/private/pilot` paths are placeholders to replace, not paths to copy literally.

```sh
mkdir -m 700 /absolute/private/pilot
git clone --branch main --single-branch \
  https://github.com/goldenfamilyfarms/sre-observability-pilot-demo.git \
  /absolute/private/pilot/demo-source
git -C /absolute/private/pilot/demo-source rev-parse HEAD
```

## 2. Generate fresh private inputs

Finish credential/project setup before generating the time-limited inputs. Replace `PILOT-123` with the real issue key, and choose one deliberate run ID. The default window is 120 minutes; `--minutes` accepts 5–240. Initial registration expects generation 0 and creates generation 1. A subsequent generation must be deliberately chosen against the current registry; do not reset persisted state to bypass a conflict.

```sh
python -m sre_suite.pilot_demo prepare \
  --checkout /absolute/private/pilot/demo-source \
  --repository goldenfamilyfarms/sre-observability-pilot-demo \
  --repository-id 1450500737 --pr-number 1 \
  --ticket-key PILOT-123 --generation 1 --run-id demo-live-001 \
  --base-branch main \
  --output /absolute/private/pilot/prepared
```

The command verifies the **committed HEAD snapshot**, including all six files and their modes, against the packaged demo. It executes only byte-verified baseline Python files in an isolated temporary directory with account credentials excluded. Uncommitted working-tree edits are not baseline evidence and are not exported to the worker. The generated revision must also equal the remote publication base; preflight checks this later.

The new owner-only directory contains:

| File | Purpose |
| --- | --- |
| `baseline.json` | Expected failing report, full scope, observation time and source digests |
| `manifest.json` | Version 2 preview registration, expected generation, expiry, baseline digest, issue key and run ID |
| `ticket-draft.json` | Human-readable requirements with **empty evidence**, intentionally not executable |
| `worker.json` | Exact repository/allowlist/test command plus private placeholders; inference and writes disabled |

An existing output is never overwritten. The [public ticket draft](../examples/demo-ticket-draft.json) illustrates the contract; its synthetic issue key and empty evidence are not ready for execution. Use the generator for current timestamps and the real key. `materialize --output NEW_PRIVATE_DIRECTORY` can export the reviewed six-file demo for local inspection; it does not create a repository, commit or PR.

- [ ] Privately fill `worker.json`: Jira URL/email/token/project/custom field; trusted absolute Claude executable, approved API model ID and API key; GitHub token scoped to the demo; real transition mapping in `SRE_JIRA_TRANSITIONS_JSON`. See [runtime configuration](execution-runtime.md) and [publication configuration](pr-and-jira-publication.md).
- [ ] Keep `SRE_REPOSITORY`, `SRE_REPO_DIR`, `SRE_GITHUB_BASE_BRANCH`, `SRE_ALLOWED_FILES_JSON` and `SRE_TEST_COMMAND_JSON` bound to this reviewed demo. Never add tests/CI to the allowlist or append `--baseline-report` to the candidate command.
- [ ] When authorizing this live pilot, set `SRE_CLAUDE_ENABLED`, `SRE_GITHUB_PRS_ENABLED`, `SRE_JIRA_WRITES_ENABLED` and `SRE_JIRA_TRANSITIONS_ENABLED` to the string `"true"`. These enable the intended actions; credentials alone do not. Keep the private file mode 0600. Do not source it as shell code or commit it.

The local launcher supplies evidence tokens, CA trust, journal, Temporal namespace and queue. Do not add or override those in the worker file. The connected GitHub account used to prepare these PRs is not the runtime worker's credential source.

## 3. Start services, register scope and upload baseline

```sh
python -m sre_suite.local_pilot init \
  --directory /absolute/private/pilot/stack \
  --manifest /absolute/private/pilot/prepared/manifest.json \
  --postgres-image postgres@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3 \
  --temporal-binary /absolute/path/to/temporal

python -m sre_suite.local_pilot up --directory /absolute/private/pilot/stack
```

Leave the services terminal running. Initialization creates exact-scope, time-limited controller and worker grants; startup applies migrations and checks PostgreSQL, Temporal and verified loopback HTTPS. From a second terminal:

```sh
python -m sre_suite.local_pilot check --directory /absolute/private/pilot/stack

python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack -- pilot-setup \
  /absolute/private/pilot/prepared/manifest.json \
  --baseline /absolute/private/pilot/prepared/baseline.json \
  --output /absolute/private/pilot/uploaded

python -m sre_suite.pilot_demo finalize-ticket \
  --prepared /absolute/private/pilot/prepared \
  --evidence /absolute/private/pilot/uploaded/baseline-evidence.json \
  --output /absolute/private/pilot/ticket.json
```

The authenticated setup registers the generation and uploads/verifies baseline bytes before producing `execution.json` and server-owned evidence metadata. Finalization checks that metadata against the full scope, content digest, times and known baseline report, then reconstructs the reviewed requirements. It ignores edited drafts. This is a local consistency check: it does not contact the API or prove that a metadata file came from a server. The mandatory later preflight verifies evidence availability, producer and bytes through the authenticated API.

- [ ] Put the complete `ticket.json` envelope into the configured Jira contract field as plain JSON text. Confirm the actual issue key, project, repository, source SHA, preview ID, generation and returned evidence match the generated execution spec. The runtime accepts legacy string values or plain ADF text paragraphs from a multiline field. Do not add rich formatting or replace evidence with placeholders.

## 4. Start a worker and inspect preflight

In another terminal, using the same virtual environment:

```sh
python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/prepared/worker.json -- worker
```

Worker startup does not create a workflow, but can resume work already queued in this namespace. Keep the foreground worker and services alive. Then:

```sh
python -m sre_suite.local_pilot check \
  --directory /absolute/private/pilot/stack --worker

python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/prepared/worker.json -- preflight \
  /absolute/private/pilot/uploaded/execution.json
```

- [ ] Resolve every failed check. Confirm the remote base SHA still matches the ticket, the current preview generation is active, baseline bytes verify, transitions are usable and the workflow ID has not already been used.
- [ ] Review unverified capabilities explicitly. Exit **2** (`needs_validation`) is the expected successful read-only result; it does not prove paid inference or write permissions and is not automatic permission to start. See [preflight](preflight.md).
- [ ] Authorize this one issue, run, model budget and non-shipping write scope. Do not change the contract, base, scope, generation or policy between inspection and start.

## 5. Run once and collect the outcome

Only after the preceding live-run authorization:

```sh
python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/prepared/worker.json -- start \
  /absolute/private/pilot/uploaded/execution.json
```

Retain the returned `workflow_id`. Replace `WORKFLOW_ID` below with that exact value:

```sh
python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/prepared/worker.json -- status WORKFLOW_ID
python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/prepared/worker.json -- result WORKFLOW_ID
```

- [ ] Temporal completes with application result `ready_for_review`, not merely a completed orchestration status.
- [ ] Implementation, review, tests and QA all pass with readable evidence bound to the same source scope and saved artifact. The unchanged strict candidate gate reports seven tests, zero failures/errors/skips; the initial missing-correlation failure has been fixed.
- [ ] The generated demo PR is open and draft, targets `main`, changes only `app.py` and preserves the reviewed baseline as its source parent. Check the actual code and PR checks. Do not merge it or the bootstrap PR as part of this pilot.
- [ ] Jira reached the configured nonterminal review status and its scoped result references the same candidate PR and evidence. It did not reach Done/Accepted or invoke shipping automation.
- [ ] Privately record issue key, workflow ID, source and candidate SHAs, preview ID/generation, baseline and stage evidence references/digests, strict test result, PR URL, Jira status, model use/cost and observed failures. Do not publish credentials, raw Jira content or private logs in the demo PR.

This outcome proves a bounded synthetic Jira-to-draft-PR run. It does not prove production readiness, independent human reviewer identities, live telemetry export, cloud preview provisioning or the 120-check service certification.

## Recovery and stop rules

On blocked/failed/canceled execution, inspect `status` and private `result`/history before retrying. A normal failed gate can move Jira to blocked; authentication, expiry or generation failure may leave it in-progress. Preserve the original workflow ID, execution journal, evidence and remote objects. An uncertain response can leave a created PR or applied Jira transition. Reconcile those exact identities; do not create a second run ID to force another write.

Restart the same local stack/worker for interrupted work only while its scope, grants, evidence and policy remain valid. Duplicate starts are refused. A changed base, expired/replaced preview or changed contract requires a newly reviewed scope and explicit registration/configuration; do not edit evidence timestamps, weaken tests or extend a grant implicitly. A local-stack restart retains state and does not renew authorization. See [publication recovery](pr-and-jira-publication.md#retry-concurrency-and-partial-completion) and [local stop/restart](local-pilot.md#stop-restart-and-recovery).

Use `cancel WORKFLOW_ID` through the local wrapper when needed, then inspect external state. Ctrl-C stops the worker/services and preserves PostgreSQL volume, Temporal state and journal. No command here merges, deploys, promotes, deletes the demo repository or silently cleans up external work.

For planner-created issues, use the [guarded handoff](planning-handoff.md) instead of manually changing a bootstrap execution spec or reusing its workflow ID.

For optional candidate-bound Prometheus/Loki/Tempo observations, follow the [engineering telemetry guide](engineering-telemetry.md). This requires a separate telemetry worker and independently authorized preview setup; it does not grant deployment permission.
