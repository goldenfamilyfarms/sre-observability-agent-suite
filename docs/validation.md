# Foundation validation

Phase 1 increment 3 has **98 core/SQLite/API tests** and **28 real PostgreSQL integration tests**. All 126 pass locally: the default suite on Python 3.14.6 and integration tests on Python 3.13.14 against a disposable PostgreSQL 17 container. Strict mypy passes for 20 source files; Ruff lint/format checks pass for 27 Python files. CI runs default checks on Python 3.11/3.14 and integration tests on PostgreSQL 16/Python 3.11 and PostgreSQL 17/Python 3.14. See the PR checks for actual remote execution status.

The new API coverage tests credential validity/audience/revocation, exact action and all scope fields, producer spoofing, strict/bounded JSON, malformed framing, generic errors, old-generation access, namespaced evidence IDs, snapshot versions, expired context, and rollback after revocation/expiry. The seven added PostgreSQL/API tests include a real loopback HTTPS connection with a temporary certificate, concurrent retries, a generation update observed waiting on PostgreSQL's advisory lock, and request-process exit after an evidence insert. The prior 91 tests retain coverage of contracts, migrations, persistence and policy. No application or cloud deployment is performed by these tests.

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

These are core tests and persistence-adapter integration tests against disposable databases. Phase 1 exercises process exit during SQLite and PostgreSQL transactions. This does not prove distributed recovery across external side effects. Test gates and ready certification definitions are synthetic. No live Claude Code, Temporal, Jira, telemetry, CI shipping, cloud or production system was exercised. The HTTP API authenticates configured service credentials and binds new upload producers to their subjects. Direct adapters still trust callers; human identity and factual claims are not verified by these tests. No service is certified or deployed by this validation.

The dynamic-environment addition is documentation only. Its Argo CD/GKE behavior references were checked against the linked official documentation; repository-relative links and whitespace were checked locally. The illustrative ApplicationSet has not been rendered with a real Helm chart, validated against an installed Argo CRD, applied to GKE, or tested for isolation/cleanup. ENV-1 through ENV-6 define the implementation and integration evidence still required.
