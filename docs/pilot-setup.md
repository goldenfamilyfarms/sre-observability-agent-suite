# Pilot setup: preview generation, baseline and execution spec

Package 0.8.0 adds `python -m sre_suite.execution.cli pilot-setup`. It registers one already authorized non-production preview generation, uploads one baseline byte file, reads it back for integrity verification, checks current scoped context, and writes a private bundle. This prepares inputs for preflight; it does not start Temporal, invoke Claude, change Jira, create a PR, provision GCP resources, promote, merge or deploy anything. Registration records an identity and validity window, not proof that a cloud environment exists.

## Operator prerequisites

1. Run the existing database migrations (still 1 and 2) and configure the authenticated HTTPS evidence API. Explicitly set `allow_preview_registration=True` when constructing `EvidenceContextApi`; the registration route returns 404 by default. The three worker routes keep their existing defaults.
2. Authorize the exact service, environment, full Git revision, preview ID and generation. `environment_id` must equal `preview-r<repository_id>-pr<pr_number>`. The numeric GitHub repository ID is stable across repository renames. The operator must verify repository/PR ownership and scope; this command does not query GitHub or infer authorization from a PR number.
3. Provision a separate controller credential with **only** `Grant(scope, Action.PREVIEW_REGISTER)`. Provision the worker credential with the exact-scope `WORKER_ACTIONS` (evidence write/read and context read). Both must be valid for the operation window. Configure credential digests on the server; deliver bearer secrets privately. No credentials are issued by setup. Do not use `for action in Action` for worker grants after this upgrade.
4. Supply `SRE_EVIDENCE_API_URL`, `SRE_PILOT_SETUP_TOKEN` (controller) and `SRE_EVIDENCE_API_TOKEN` (worker) in the setup process environment. The two tokens must differ. Do not pass the controller credential to worker processes. Runtime Jira/Temporal/model settings are not required by setup. Remote HTTPS must use a credential-free URL on port 443; redirects are refused. The [local startup package](local-pilot.md) permits an unprivileged loopback port with explicitly configured certificate trust.
5. Prepare sanitized baseline bytes and a manifest with their SHA-256 digest. Review the contents before uploading: digest verification proves byte integrity, not the truth of an observation or the absence of confidential material. Keep real manifests, evidence and output outside public source control.

## Manifest

This illustrative JSON uses synthetic identities and dates. Replace them with an explicitly authorized scope and a current validity window. The digest shown is for the exact UTF-8 bytes `synthetic baseline` without a newline. The CLI rejects unknown/missing fields, duplicate keys, non-finite JSON, coerced integer types and oversized inputs.

```json
{
  "schema_version": 2,
  "registration": {
    "schema_version": 2,
    "preview": {
      "repository_id": 1,
      "pr_number": 1,
      "scope": {
        "service": "demo-service",
        "environment": "development",
        "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "environment_id": "preview-r1-pr1",
        "generation": 1
      },
      "requested_at": "2026-11-08T12:00:00+00:00",
      "expires_at": "2026-11-08T12:30:00+00:00"
    },
    "expected_generation": 0
  },
  "ticket_key": "DEMO-1",
  "run_id": "pilot-1",
  "baseline": {
    "sha256": "9a5b038b289c630fcf58c973340d6f142b1e976b263cce4238c5cbad195b7e7c",
    "observed_at": "2026-11-08T12:00:00+00:00",
    "expires_at": "2026-11-08T12:20:00+00:00"
  }
}
```

`expected_generation` must be exactly `generation - 1`. Use 0 only for first registration. The registry compares the expected generation against stored state under its transaction lock. An identical request for the current open generation is a successful retry; a changed request for that generation, closed generation, stale generation or skipped generation fails. Service and environment class cannot change for an existing preview ID. Production is rejected. Registration checks server time; setup also checks local time, so synchronize clocks. Evidence must be currently valid and expire no later than the preview. The 32 KiB manifest and 1 MiB baseline limits are checked before remote writes.

## Run and consume the result

Create a private parent directory outside any Git checkout. Use a physical absolute path without symlink components (on systems where `/tmp` is a symlink, use its resolved path). The output directory is created by the command; do not precreate it.

```sh
mkdir -m 700 /absolute/private/pilot
python -m sre_suite.execution.cli pilot-setup /absolute/private/manifest.json \
  --baseline /absolute/private/baseline.bin \
  --output /absolute/private/pilot/prepared
```

The existing parent must belong to the current user and have no group/other permissions. Output is published as a complete directory with mode 0700 and files with mode 0600:

- `execution.json`: version 2 execution spec accepted by the existing `preflight` and `start` commands. It preserves the exact scope, ticket key and run ID.
- `baseline-evidence.json`: version 2 evidence envelope containing the server-assigned ID and authenticated producer. Put its `evidence` object into the Jira contract's `ticket.evidence` array. Keep the other contract fields, acceptance criteria and exact scope consistent; this command does not manufacture a Jira ticket or modify its custom field.
- `receipt.json`: setup fingerprint and deterministic workflow ID. The fingerprint binds the manifest, baseline digest and evidence API URL. No bearer secrets, baseline bytes or local source paths are included in these files.

Exit 0 prints `status: prepared`, a workflow ID and `execution_started: false`. After configuring the validated Jira contract and runtime environment, run:

```sh
python -m sre_suite.execution.cli preflight /absolute/private/pilot/prepared/execution.json
```

Preflight remains read-only and may return exit 2 for live capabilities that cannot be proven by reads. Starting work is a separate explicit action; a prepared bundle is not a readiness certificate, a reserved generation or permission to deploy. Every runtime activity still checks current preview scope and evidence.

## Failure and retry behavior

Exit 1 returns only the failing phase (`validation`, `registration`, `baseline_upload`, `verification`, `output`) and a conservative `remote_changes_may_exist` flag. Raw exceptions, credentials, response bodies and file contents are not printed. Interruptions return 130. Registration and upload are separate transactions: an interrupted/failed setup may leave a registered preview or uploaded evidence. It does not delete evidence, roll back a generation, extend expiry, automatically increment the generation or start compensating cloud actions.

Retry the **same manifest, baseline, API URL and output directory**, using credentials for the same authorized subjects. Upload IDs are deterministic and the API makes identical writes idempotent, including a lost response after commit. Output retries validate existing private files and reject conflicting requests rather than overwrite them. A local file lock serializes commands targeting the same output; complete bundles are atomically renamed into place and synced. Filesystems must support POSIX file locks, atomic directory rename and fsync. These are local filesystem guarantees, not distributed storage guarantees. A process crash may leave a private `.pilot-pending-*` staging directory; an operator may remove an abandoned staging directory after confirming no command is active. Do not delete the lock file while commands are active.

A stale/expired/closed preview requires operator reconciliation and new exact-scope authorization where appropriate. Changing scope, dates or run ID is a new setup request, not an automatic retry; use a new output directory. No fresh bundle is published when verification fails; an earlier successful bundle remains on disk and may since have expired or been superseded. Final verification is point-in-time: another controller can change the generation after the final read. Preflight and runtime revalidation remain mandatory; the bundle does not hold a lease across external operations.

## Registration API

`POST /v1/previews/register` accepts exactly the manifest's `registration` object and returns the same validated version 2 shape on success (200). It uses `preview:register`, separate from the worker permissions. Invalid input returns 400, missing/invalid credential 401, missing exact-scope permission 403, disabled route 404 and conflicting/inactive registration 409. The API authorizes before opening storage, acquires the existing shared database generation lock, registers and audits in one transaction, then checks authority and current scope again before commit. Revocation, expiry or backend failure rolls back both preview and event. SQLite and PostgreSQL use the same route and domain logic. There is no generation discovery/listing, credential management, automatic migration or release-approval endpoint.
