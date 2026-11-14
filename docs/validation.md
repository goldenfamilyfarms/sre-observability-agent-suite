# Foundation validation

Version 0.12 adds 18 guarded-handoff tests: exact receipt/journal/Jira mapping, real scoped evidence, prerequisite acceptance, final fences, read-only adapter enforcement, private outputs, preflight exit codes and local credential separation. The default suite now contains 270 tests; the complete matrix contains 315 distinct tests with the unchanged 31 PostgreSQL, 12 Temporal/container and 2 local startup checks. See current PR checks for executed results.

Version 0.11 adds 22 Jira planning tests, bringing the default suite to 252 and the complete matrix to 297 distinct tests (31 PostgreSQL, 12 Temporal/container and 2 local startup tests). The new tests use the real scoped evidence API and synthetic Jira responses; they exercise planning, publication uncertainty, concurrent retries, generation fences and ADF execution-reader compatibility. Local and remote results are recorded in the PR; no live Jira/model actions are part of validation.

Phase 1 increment 9 has **230 core/SQLite/API/runtime tests**, **31 PostgreSQL integration tests**, **12 Temporal/container integration tests**, and **2 local startup acceptance tests** (275 distinct tests). Locally, all 230 default tests pass on Python 3.14.6 and all 12 engine tests pass on Python 3.13.14. PostgreSQL and full local startup regressions run in CI on Python 3.11/3.14, alongside default and engine tests. Strict typing, lint/format checks and installed-package asset checks are included. See PR checks for actual remote results; configuration alone does not prove a run succeeded.

Increment 9 adds 14 default tests and two real-container tests for the reviewed demo. They check exact committed source provenance (including unexpected shadow modules and Git attributes), expected baseline failure, a correct implementation passing all seven unchanged checks, invalid/hardcoded trace IDs failing, immutable test allowlists, exact preview/revision/generation and evidence digest/time binding, private non-overwriting outputs, disabled worker flags, edited-draft rejection and redacted errors. Ticket finalization is tested against metadata returned by the real local authenticated API with synthetic grants. The demo repository has its own Python 3.11/3.14 baseline CI. No live Jira/model credentials are used.

The increment 4 default tests cover Jira contract/project/repository constraints, environment configuration, tool/file limits, model result reuse, artifact integrity, evidence-to-artifact binding, generation replacement before and during model work, failed QA gates, process cancellation/time/output bounds, patch applicability, and TLS requirements. Engine tests prove workflow replay, restart after a saved stage loses its acknowledgement, duplicate workflow rejection, cancellation and bounded retries. The complete real-engine path executes candidate tests in Docker, publishes a draft PR and transitions Jira, while GitHub/Jira responses and Claude completions use fixtures. Container tests also demonstrate that the unmodified baseline fails while the proposed change passes, plus read-only/non-root/no-route execution without runtime secrets.

The new API coverage tests credential validity/audience/revocation, exact action and all scope fields, producer spoofing, strict/bounded JSON, malformed framing, generic errors, old-generation access, namespaced evidence IDs, snapshot versions, expired context, and rollback after revocation/expiry. The seven added PostgreSQL/API tests include a real loopback HTTPS connection with a temporary certificate, concurrent retries, a generation update observed waiting on PostgreSQL's advisory lock, and request-process exit after an evidence insert. The prior 91 tests retain coverage of contracts, migrations, persistence and policy. No application or cloud deployment is performed by these tests.

Increment 5 adds 24 publication tests: exact Git candidate contents/parent/modes, loss of Git object/ref/PR/transition/report responses, concurrent duplicate publication, failed gates, generation changes before/between/after writes, expiry, cross-preview rejection, moved base/head, edited/closed PRs, terminal statuses, marker conflicts and GitHub-token isolation. The real Temporal test also loses PR and transition responses before successful reconciliation and history replay.

Increment 6 adds 27 readiness tests covering read-only transport enforcement, credential/report redaction, scope/expiry/final fences, prerequisite and permission failures, model metadata, journal reuse, deadline handling and CLI exit codes. Three real Temporal tests verify describe-only connectivity, queue pollers, missing namespaces/workers and duplicate workflow IDs without preflight creating workflows.

Increment 7 adds 25 default tests and three PostgreSQL tests for controller/worker authority separation, opt-in routing, strict IDs/generations/windows, idempotent/concurrent registration, competing revisions, transactional revocation/expiry/failure rollback, setup retries after lost registration/upload responses, stale generations, private atomic output, immutable output conflicts, API-origin binding, CLI redaction, existing Jira contract compatibility and preservation of read-only preflight. These tests exercise the real API with synthetic credentials, not a live evidence service or operator account.

Increment 8 adds 22 default tests and two real startup tests. They cover separate scoped credentials, private/atomic initialization, strict ports, refusal of remote Docker contexts and immutable state, credential reload/revocation, environment isolation, local TLS trust without disabling verification, process/volume ownership, cancellation and cleanup failure handling. Live local tests run the command wrapper through verified HTTPS, reject untrusted TLS/plaintext/duplicate framing, confirm worker pollers, and retain PostgreSQL evidence/preview events and a Temporal workflow identity across a complete restart. The pending test workflow uses an unused queue; no external Jira/model calls occur.

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

## Telemetry collection increment (0.13.0)

Local validation on 2026-11-14: 304 default tests passed, including 34 new telemetry tests. Four additional loopback HTTPS tests passed with synthetic responses over real TLS. Strict mypy passed for 57 source files; Ruff lint and formatting passed for 81 Python files. The wheel built offline and installed into a clean environment; the telemetry command and synthetic demo ran outside the checkout. Example policy/request parsing and whitespace checks passed.

The new tests exercise exact scope mappings, query encoding, time/sample/byte limits, nonfinite and unsupported data, warning/limit/empty semantics, duplicate identities, typed records, credential/tenant validation, private CLI output, real authenticated evidence upload/readback, preview closure and generation/context changes before, during and after collection. HTTPS tests check actual GET parameters and headers, certificate verification, redirect refusal, errors, duplicate JSON and byte limits. Source responses are fixtures; no real Prometheus, Loki or Tempo deployment was queried. Full trace retrieval, durable telemetry scheduling, artifact/plan attribution and 120 deterministic certification assertions remain open.

CI now runs the telemetry HTTPS suite in both Python validation jobs in addition to the existing PostgreSQL, Temporal/container and local-startup suites. Refer to the PR checks for actual remote results; this paragraph does not assert that configured jobs have run.

## Full trace and durable telemetry increment (0.14.0)

Local validation on 2026-11-14: 332 default tests passed, including 28 new full-trace/schedule tests; six real loopback HTTPS tests passed, including two new V2 trace cases. Seven new real Temporal tests passed for timers, replay, duplicate rejection, bounded retries, saved-result recovery, stale generations, timer/active cancellation and process deadlines. A cancellation-classification failure found by the active-process test was corrected and the complete seven-test group rerun successfully. Strict mypy passes for 65 source files and Ruff lint/format for 92 Python files.

Trace fixtures include modern/legacy OTLP layouts, hex/base64 identities, attributes/events/links, partial/pruned/dropped traces, cross-service context, invalid graphs, exact scope/window checks and evidence round trips. Scheduling uses real authenticated SQLite evidence and synthetic source responses; process-supervision tests use disposable blocking children. No live Tempo, private account, Jira/model operation, merge or deployment is involved. CI runs all existing integration suites and installed-package commands; consult the current PR checks for remote results.

## Engineering telemetry binding increment (0.15.0)

Local validation on 2026-11-14: 347 default tests passed (15 new), six loopback HTTPS tests passed, and 13 focused real Temporal/process tests passed (six new, seven existing), plus one private/exclusive plan-export test passed. New coverage includes all three source bindings, actual authenticated evidence reads, altered/missing plans and labels, partial/empty observations, expiry and generation fences, draft publication recovery/head checks, child workflow replay, lost acknowledgements, parent restart/cancellation, failed children and the process receipt boundary. Strict mypy passes for 66 source files and Ruff lint/format for 95 Python files. Full PostgreSQL/container/local-pilot matrix results are reported on the PR checks.

All service responses, credentials and repository fixtures are synthetic. No external Jira/model/backend calls, merge or deployment were performed. A source-candidate digest is not deployed-image provenance; query collection is not deterministic certification. See [the integration guide](engineering-telemetry.md) for operator setup and remaining live acceptance.
