# Foundation validation

Phase 1 increment 2 retains the **70 core/SQLite tests** and adds **21 real PostgreSQL integration tests**. All 91 pass locally: the core suite on Python 3.14.6 and PostgreSQL tests on Python 3.13.14 against a disposable PostgreSQL 17 container. Strict mypy passes for 16 source files; Ruff lint/format checks pass for 21 Python files. The synthetic demo still passes without external calls. The optional PostgreSQL package includes both SQL migrations.

New coverage includes concurrent migration, upgrade preservation, checksummed history, failed-DDL rollback, database scope guards, simultaneous generation/checkpoint/knowledge updates, immutable evidence content, scoped reopen/replay, repeatable-read snapshots, stale worker results, transactional audit writes and a subprocess exiting during a PostgreSQL preview update. See the [PostgreSQL guide](postgresql.md) for commands and recovery limits. CI adds PostgreSQL 16/Python 3.11 and PostgreSQL 17/Python 3.14 jobs alongside the existing core checks. The PR checks supply actual remote results; the workflow definition alone is not evidence of a run.

## Phase 0 baseline results

Local validation on 2026-11-07 used Python 3.14.6, mypy 1.19.1 and Ruff 0.14.10.

| Check | Result |
| --- | --- |
| `python -m unittest discover -v` | 39 tests passed, including negative cases and SQLite adapter tests |
| `python -m sre_suite.demo` | Synthetic worker result validated; checkpoint and knowledge restored after storage reopen; no deployment |
| `python -m mypy sre_suite` | Strict checking passed for 9 source files |
| `python -m ruff check .` | Passed |
| `python -m ruff format --check .` | Passed for 12 Python files |
| Build and install into a clean virtual environment | Passed; packaged catalog included |

CI runs the same checks on Python 3.11 and 3.14 and exercises the installed package outside the checkout. See the PR checks for actual remote execution status; adding workflow configuration does not itself prove CI ran.

## Test coverage by behavior

- Contract completeness, explicit environment/revision, evidence scope and URI/time validation.
- Deterministic dependency ordering and rejection of cycles, duplicates and missing prerequisites.
- Default-deny promotion, exact non-production authorization, required gates, human production approval, expiry/revocation and artifact/plan/revision/gate-set substitution.
- Proposed findings excluded from canonical context; persistent facts/graph and events; conflict detection; stale-writer rejection across connections; idempotency conflicts; evidence expiry and scope isolation.
- Ticket ordering, dependency guards, failed verdict rejection, reviewer/QA/verifier separation, terminal/blocked states and evidence freshness.
- Checkpoint reopen, duplicate operation replay, mismatched operation payload and optimistic write conflict.
- Worker execution/context binding and bounded request attempts; synthetic context/worker/checkpoint slice.
- 120 unique slots and category counts; complete synthetic reports; draft refusal; missing/duplicate/unknown checks; query success without assertion; mandatory failure; ERROR/INCONCLUSIVE; justified non-applicability; artifact/version/window/evidence mismatch.

## Limits

These are core tests and persistence-adapter integration tests against disposable databases. Phase 1 exercises process exit during SQLite and PostgreSQL transactions. This does not prove distributed recovery across external side effects. Test gates and ready certification definitions are synthetic. No live Claude Code, Temporal, Jira, telemetry, CI shipping, cloud or production system was exercised. Phase 1 verifies local evidence content hashes, but producer/human identity and factual claims are not authenticated by these adapters. No service is certified or deployed by this validation.

The dynamic-environment addition is documentation only. Its Argo CD/GKE behavior references were checked against the linked official documentation; repository-relative links and whitespace were checked locally. The illustrative ApplicationSet has not been rendered with a real Helm chart, validated against an installed Argo CRD, applied to GKE, or tested for isolation/cleanup. ENV-1 through ENV-6 define the implementation and integration evidence still required.
