# Certification in the engineering workflow

Version 0.17 adds an optional required certification gate to `TicketExecution`. A private operator configuration enables it before an execution starts. A Jira ticket, retrieved text, model response or Temporal signal cannot enable or weaken the gate. Existing executions without the recorded requirement retain their original workflow path; changing an active execution's policy blocks reuse.

```text
four passing engineering stages
  -> frozen candidate-bound telemetry plan + certification plan
  -> bounded telemetry child workflows
  -> independent fixture evidence reference (bounded wait)
  -> authenticated immutable certification input
  -> ServiceCertification child on its own queue
  -> current certificate/evidence/generation validation
  -> guarded review readiness and optional Jira in-review transition
```

The existing telemetry stage may publish an authorized **draft** before collection, so independently authorized preview tooling can obtain the candidate. Certification gates review readiness; it does not prevent that provisional draft. No workflow here merges, builds an image, provisions a preview or deploys. Production approval and deployed-image provenance remain separate requirements.

## Private configuration

First prepare [engineering telemetry](engineering-telemetry.md) and a fully reviewed [120-check verification policy](certification.md). Store a second owner-only file outside Git with these exact fields:

```json
{
  "schema_version": 2,
  "policy": "REPLACE with the complete reviewed verification-policy object",
  "task_queue": "sre-certification-v1",
  "fixture_wait_seconds": 300
}
```

This illustration is intentionally invalid until `policy` is replaced with an object. Export `SRE_ENGINEERING_CERTIFICATION_CONFIG=/absolute/private/engineering-certification.json` alongside `SRE_ENGINEERING_TELEMETRY_CONFIG`. `check-config` and preflight validate the private configuration; they do not start certification or prove the assessor/verifier's live permissions. The local-pilot wrapper preserves the new configuration variable but does not automatically launch a verifier worker or provision its identity.

The embedded verification policy must exactly match the execution scope and telemetry collector subject. Its source set must equal the selected engineering collection profiles, including query, endpoint, scope/artifact mappings, full trace ID, step (15 seconds) and limit (100). This integration currently inherits engineering telemetry's one-to-three profiles, one per backend. Those observations must actually cover the reviewed assertions for all 120 slots. Configurations needing additional sources must use standalone certification until the engineering collector supports those sources; they are rejected rather than silently dropped. Applicability remains an independently assessed policy decision, with core checks always required.

Run the separate verification worker with the **same embedded policy** saved as a standalone policy file, a separate durable owner-only journal and its own verifier API credential:

```sh
python -m sre_suite.verification worker \
  --policy /absolute/private/certification-policy.json \
  --journal /absolute/private/certification-journal
```

Set `SRE_CERTIFICATION_TASK_QUEUE` in that process to the configured queue, and use the same Temporal service/namespace. Keep backend credentials on telemetry workers; keep Jira/GitHub/model credentials on the engineering worker. The verifier process retains only its evidence API/TLS environment. Configure exact-scope evidence reads across the assessor, collector and verifier records. The engineering worker needs permission to read the fixture and certificate, but does not mint either under the independent subjects. No production credential is needed for this path.

Starting the engineering workflow remains explicit through the existing `execution.cli start` command. With this configuration, that start authorizes the bounded certification child and report upload after the engineering gates; there is no second automatic launch mechanism.

## Supply independent fixture evidence

The workflow publishes a read-only plan query before collecting telemetry. Export it to a new private file:

```sh
python -m sre_suite.execution.cli export-certification-plan WORKFLOW_ID \
  --output /absolute/private/certification-plan.json
```

The plan contains the exact `release`, artifact kind, window, context version, catalog/policy fingerprints and telemetry-plan digest. The artifact is the existing `source-candidate-v1` recipe; the release's plan digest is the frozen engineering manifest. It does not identify a deployed OCI image. An independent assessor uses these fields to produce the full fixture/applicability document described in the [certification guide](certification.md), with all 120 assessment IDs, and uploads it with the assessor's authenticated evidence credential. This increment does not invent expected values, generate fixture traffic or implement a fixture-runner agent.

Save the API-returned Evidence metadata object (not just its ID and not the fixture document) in a `0600` file outside Git, then submit it:

```sh
python -m sre_suite.execution.cli submit-certification-fixture WORKFLOW_ID \
  --evidence /absolute/private/fixture-reference.json
```

The command signals only metadata and starts no workflow. Submission acknowledgement is not verification. The first bounded reference is frozen in workflow history; duplicate submissions are harmless and later different references are ignored. Protect Temporal signal access with operator/assessor authorization: an invalid first submission can block a run, but cannot grant readiness. Use a newly reviewed run ID after correcting an invalid submission; never overwrite saved inputs.

The wait begins after successful collection and lasts `fixture_wait_seconds` (1–600). The assessor can submit earlier once its completed-window evidence exists. Evidence observation time must be from window end through 120 seconds afterward and must remain unexpired. Missing fixtures time out to a blocked result; failed/partial collection skips verification. Wrong producer, candidate, plan, window, preview ID/generation, policy or context blocks input binding. The saved input binds the actual collection receipts; operators cannot substitute an arbitrary standalone certificate.

## Gate, recovery and limits

The parent starts one `ServiceCertification` child, awaits its result and checks actual certificate bytes through the engine's gate reader. Only a complete PASSED result can permit review readiness. FAILED, INCOMPLETE, timeout, malformed receipt or unavailable verifier cannot pass. The parent rechecks the saved stages, ticket, exact telemetry, certificate, input evidence freshness, current preview generation/context and draft head around result publication and Jira transitions. Historical PASSED receipts are insufficient.

Plans, windows and inputs are immutable journal records. Lost activity/report acknowledgements reuse the same input identity and bytes; worker restart and workflow replay preserve the plan and accepted fixture. Parent cancellation requests child cancellation and waits for acknowledgement; hard child/process deadlines remain in force. Already published drafts and evidence are retained. Final report retries recheck current authority even when remote Jira writes have already succeeded.

Run the parent with `status`/`result` as usual. Successful reports include a `certification` summary with the authenticated evidence reference and exact release. Failed certification summaries carry no passing gate; activity/child failures can return a blocked workflow result with `jira_reported=false`. Manual reconciliation may be needed after expiry or generation change. Live backend/schema acceptance, independent fixture automation, cross-host journal recovery, shipping and human production approvals are not established by synthetic tests.
