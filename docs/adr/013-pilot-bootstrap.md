# ADR-013: Explicit, replayable pilot setup

Status: accepted for the bounded Phase 1 pilot implementation. Live account acceptance and infrastructure provisioning remain separate.

## Context

Execution and read-only preflight require a registered current preview and authenticated baseline evidence. Requiring direct database edits for every pilot obscures controller authority and makes partial setup hard to retry. Workers must not gain authority to advance generations as a side effect of an API upgrade.

## Decision

Add an opt-in `/v1/previews/register` endpoint using the existing authenticated HTTPS boundary and cooperative database generation lock. Require a new exact-scope `preview:register` controller grant; keep the default three worker routes and enumerate worker permissions explicitly with `WORKER_ACTIONS`. Registration uses the existing compare-and-set generation contract and idempotent exact request matching. No database migration is needed.

Add a `pilot-setup` command with an immutable manifest, an explicitly selected bounded baseline file and a private output directory. Require separate controller and worker credentials. Validate all local inputs before remote writes, register the requested generation, upload using a deterministic ID, verify stored bytes and current scope, then publish an atomic private bundle containing the execution spec, server-owned evidence reference and receipt. Bind the request fingerprint to the evidence API origin. Do not infer Jira fields, issue credentials, allocate a generation, start a workflow or create cloud resources.

## Consequences

Registration and evidence upload are independently durable; uncertain writes are retried with identical inputs. No compensating generation rollback or evidence deletion is attempted. The API rechecks scope and authority before commit and rolls back preview/audit writes on observed revocation or expiry. Setup output is a point-in-time preparation artifact; the existing preflight and runtime fences must still reject superseded scopes. There is no cross-service transaction or generation lease through local publication.

The command targets POSIX operator hosts with private directories, local file locking, atomic rename and fsync. A separately authorized operator supplies repository/PR identity and grants and reviews baseline contents before upload. Output does not establish real telemetry applicability, certification, independent review authority or live runtime readiness. PostgreSQL concurrency/rollback tests and real-API setup retry tests cover the boundary; external production services are not contacted by those tests.
