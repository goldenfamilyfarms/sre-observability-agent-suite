# Foundation validation

Phase 1 increment 5 has **142 core/SQLite/API/runtime tests**, **28 PostgreSQL integration tests**, and **7 Temporal/container integration tests**. Locally, the 142 default tests pass on Python 3.14.6 and the 7 engine tests pass on Python 3.13.14 using Temporal SDK 1.35.0, a temporary Temporal CLI 1.10.0/server 1.32.2, and the pinned Python test image. The unchanged 28 PostgreSQL tests passed in increment 3 against PostgreSQL 17 and run again in this PR's CI. Strict mypy passes for 35 source files; Ruff lint/format checks pass for 48 Python files. CI covers Python 3.11/3.14, PostgreSQL 16/17, and real Temporal/container tests. See PR checks for actual remote results.

The increment 4 default tests cover Jira contract/project/repository constraints, environment configuration, tool/file limits, model result reuse, artifact integrity, evidence-to-artifact binding, generation replacement before and during model work, failed QA gates, process cancellation/time/output bounds, patch applicability, and TLS requirements. Engine tests prove workflow replay, restart after a saved stage loses its acknowledgement, duplicate workflow rejection, cancellation and bounded retries. The complete real-engine path executes candidate tests in Docker, publishes a draft PR and transitions Jira, while GitHub/Jira responses and Claude completions use fixtures. Container tests also demonstrate that the unmodified baseline fails while the proposed change passes, plus read-only/non-root/no-route execution without runtime secrets.

The new API coverage tests credential validity/audience/revocation, exact action and all scope fields, producer spoofing, strict/bounded JSON, malformed framing, generic errors, old-generation access, namespaced evidence IDs, snapshot versions, expired context, and rollback after revocation/expiry. The seven added PostgreSQL/API tests include a real loopback HTTPS connection with a temporary certificate, concurrent retries, a generation update observed waiting on PostgreSQL's advisory lock, and request-process exit after an evidence insert. The prior 91 tests retain coverage of contracts, migrations, persistence and policy. No application or cloud deployment is performed by these tests.

Increment 5 adds 24 publication tests: exact Git candidate contents/parent/modes, loss of Git object/ref/PR/transition/report responses, concurrent duplicate publication, failed gates, generation changes before/between/after writes, expiry, cross-preview rejection, moved base/head, edited/closed PRs, terminal statuses, marker conflicts and GitHub-token isolation. The real Temporal test also loses PR and transition responses before successful reconciliation and history replay.

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

Tests include disposable databases, a real local Temporal engine and constrained Docker execution. Phase 1 also exercises process exit during SQLite and PostgreSQL transactions. The saved-result/lost-acknowledgement test restarts a worker, not the Temporal server or an entire host. Cross-host filesystem recovery and server disaster recovery remain unproved. Jira and Claude responses and certification definitions are synthetic. No live Jira issue, paid Claude completion, telemetry, CI shipping, cloud or production system was exercised. The HTTP API authenticates configured service credentials and binds new upload producers to their subjects. Direct adapters still trust callers; human identity and factual claims are not verified by these tests. No service is certified or deployed by this validation.

The dynamic-environment addition is documentation only. Its Argo CD/GKE behavior references were checked against the linked official documentation; repository-relative links and whitespace were checked locally. The illustrative ApplicationSet has not been rendered with a real Helm chart, validated against an installed Argo CRD, applied to GKE, or tested for isolation/cleanup. ENV-1 through ENV-6 define the implementation and integration evidence still required.
