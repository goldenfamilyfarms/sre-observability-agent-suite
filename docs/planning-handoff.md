# Guarded planning-to-pilot handoff

Version 0.12 connects an explicitly published Jira plan to the existing execution preflight. It generates a specification for one selected planned node using the **actual published Jira key**, checks its evidence and preview generation, and runs read-only preflight. It never starts a workflow, creates/edits an issue, uploads evidence, invokes a model completion, merges or deploys.

## Inputs

Retain the reviewed plan, publication result and original private planning journal. The publication JSON is the exact output from `jira_planning publish` (`outcome=tickets_prepared`). Save it privately, for example by redirecting that command's standard output to a new owner-only file. Do not reconstruct its issue mapping from memory. Every node must have a completed entry in the retained journal; a partial or uncertain publication must be reconciled first.

The handoff verifies the receipt's destination-bound plan digest and exact planned-ID-to-key mapping against the journal. It reads every remote issue's identity, marker and contract, and validates the selected ticket through the normal execution adapter. The selected issue must remain nonterminal. A completed prerequisite is allowed only when the runtime's configured accepted-status check passes; handoff never marks prerequisites accepted. The complete service/environment/revision/preview ID/generation and evidence digest/time checks remain active.

Both remote contracts and the journal are checked again after preflight. A changed generation, changed contract, incomplete journal, foreign/duplicate key, mismatched report identity or altered temporary spec prevents the output bundle from being published. Journal reads do not alter publication state or acquire a new execution lease. This is an observation; runtime guards still apply at start and every execution boundary.

## Local stack command

Keep the existing services and worker running. Use the private worker configuration already used by that worker, plus the same issue type used to publish the plan:

```sh
python -m sre_suite.local_pilot handoff \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/worker.json \
  --plan /absolute/private/plan.json \
  --publication /absolute/private/publication.json \
  --planning-journal /absolute/private/planning-journal \
  --issue-type-id 10001 \
  --node instrumentation --run-id pilot-one \
  --output /absolute/private/handoff-one
```

Replace illustrative paths, issue type and node/run IDs with reviewed values. The local wrapper supplies the exact stack's evidence token, CA, execution journal, namespace and queue. It excludes ambient credentials, the preview controller token and database password. The planning issue type is an explicit argument; no new settings or privileges are added to the engineering worker file. No service or worker process is started by this command.

For a separately hosted stack with private environment variables already exported:

```sh
python -m sre_suite.jira_planning handoff /absolute/private/plan.json \
  --publication /absolute/private/publication.json \
  --journal /absolute/private/planning-journal \
  --node instrumentation --run-id pilot-one \
  --output /absolute/private/handoff-one
```

This form requires the planning destination settings (including `SRE_JIRA_ISSUE_TYPE_ID`) and the normal runtime settings. Jira/evidence adapters are wrapped in read-only transports even if write flags are enabled. The wrapper permits GET requests and the existing read-semantic evidence/context POSTs only. Runtime policy is left unchanged so preflight observes the configuration intended for the subsequent explicit start.

## Output and exit codes

The new output directory must use a physical absolute path with an owner-only parent outside every Git checkout. Existing directories are never overwritten, including an earlier handoff for the same run. Use a new directory for a fresh observation, not a new run ID to bypass a retained workflow or failed guard.

| File | Contents |
| --- | --- |
| `execution.json` | Version 2 specification with actual Jira key, full scope and chosen run ID |
| `preflight.json` | Redacted read-only report for that exact execution identity |
| `handoff.json` | Plan digest, selected node, context version, workflow ID, spec/report digests, status and `execution_started=false` |

The bundle is written atomically with private permissions after validation. Credentials and raw Jira/evidence bodies are not copied into it. Temporary specifications are private and removed after preflight.

Exit codes preserve the preflight contract: **1** for blocked, **2** for `needs_validation`, **0** for passed. The current pilot normally returns 2 because paid inference and write capabilities remain unverified. A blocked preflight can still produce a diagnostic bundle if the publication/scope fences remain valid. A failed handoff consistency check produces no bundle. Neither exit 0 nor 2 grants execution permission.

## Explicit workflow start

Review `preflight.json`, resolve failures and authorize the selected live run. Only then use the separate existing start command:

```sh
python -m sre_suite.local_pilot run \
  --directory /absolute/private/pilot/stack \
  --worker-env /absolute/private/pilot/worker.json -- start \
  /absolute/private/handoff-one/execution.json
```

Use `status WORKFLOW_ID` and `result WORKFLOW_ID` through the same wrapper, **including `--worker-env`**. The workflow ID comes from the handoff receipt/start result, not the earlier bootstrap receipt that may have used a different ticket key. The runtime refuses duplicate starts and revalidates scope/evidence independently. Handoff does not reserve the run ID or mutate execution history.

Keep the source base stable. Demo bootstrap PR #1 has been merged; the reviewed demo now uses `main` at `a492698b84cc4cda04d5e64d3f638a460827e129` (observed 2026-11-14). Recheck the current base before generating fresh inputs. Select an explicitly authorized preview identity/generation; handoff does not choose or provision one from PR status. Old observation windows must be regenerated, never edited to extend evidence validity.

## Verification and limits

Tests exercise the real scoped evidence API and planning journal with synthetic Jira responses, plus the real preflight implementation with fixture network/process probes. They cover exact mapping/spec generation, prerequisite acceptance, generation and journal/contract changes during preflight, foreign reports, exception cleanup, immutable private outputs, read-only transport enforcement, local credential separation and exit-code propagation. There are no live Jira/model calls in this validation. A preflight result remains a point-in-time observation; it is neither a shipping gate nor proof of all-agent/service certification.
