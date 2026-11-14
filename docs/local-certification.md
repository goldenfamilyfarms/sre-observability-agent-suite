# Full certification against the local telemetry stack

Version 0.22 connects the [real local backends](local-telemetry.md) to the full engineering certification workflow. A private preparation command derives mutually consistent managed-demo, fixture, collection and verification configuration from an already reviewed 120-check policy. It never starts a workflow. Assertions are preserved unless the operator explicitly selects the reviewed `--demo-instrumentation` policy.

## Prepare and review the complete bundle

First initialize the local stack and prepare a managed-demo policy using the [managed pilot guide](managed-demo-pilot.md). That input must already contain all 120 reviewed checks, independent expected values, capability assessments, the exact source recipe and a fixed full trace ID. The stack and policy must have identical scope (including revision, preview ID and generation), target origin and pinned local target certificate. This command supports the self-signed local target certificate used by the pilot, not arbitrary CA-chain rebinding.

```sh
python -m sre_suite.local_telemetry prepare-certification \
  --directory /absolute/private/telemetry \
  --managed-policy /absolute/private/reviewed-managed-demo.json \
  --output /absolute/private/certification \
  --window-seconds 30
```

All paths must be absolute, private, outside Git and free of symlinks. Output must not already exist. Preparation validates the entire bundle before publishing it atomically. It performs no network calls, image pulls, preview registration, credential issuance or workflow start. It prints only fingerprints/counts and `execution_started: false`.

The command changes source transport fields to the local stack's origins, role-specific credential variable names and explicit loopback CA. It also changes the managed export destinations. It preserves queries, source IDs, labels, step/limit, full trace ID, all rules/selectors/thresholds, scenario expectations, applicability, identities and source recipe. Review the new fingerprints before authorizing a fresh run: existing execution plans and evidence cannot adopt the changed policy.

| Generated file | Use |
| --- | --- |
| `review.json` | Compare original/new policy fingerprints and source/check counts |
| `engineering-env.json` | Private engineering worker configuration paths; merge these values into its environment |
| `engineering-telemetry.json` | Reviewed collection profiles, resolution/limit, collector identity, queue and fixed window |
| `engineering-certification.json` | Full verification policy and matching fixture/lifecycle dispatch fingerprints |
| `query-policy.json` | Dedicated telemetry worker policy |
| `verification-policy.json` | Dedicated verifier worker policy |
| `fixture-policy.json` | Independent runner and assessor policy |
| `managed-demo.json` | Dedicated lifecycle worker policy and local export destinations |

The bundle contains no copied credentials. Use `collector-env.json` and `export-env.json` from the telemetry stack for their respective workers, plus the existing independent scoped evidence credentials. The collector, runner, assessor and verifier remain distinct authenticated subjects. Keep the five activity queues separate; override `--collector-queue` and `--verifier-queue` if the defaults conflict with your existing queues.

## Multiple bounded queries and fixed observation settings

Engineering collection now accepts up to 20 uniquely named profiles, including multiple profiles for one backend. Each `profiles` entry may specify both `step_seconds` (1–3600) and `limit` (1–500). Omitting both preserves the legacy defaults of 15 seconds/100 records and its engineering configuration fingerprint. Explicit nondefault options enter the fingerprint and immutable schedule. The verifier checks exact source, trace ID, resolution and limit equality against the collection configuration.

For example, use separate Prometheus profiles for request counters, duration buckets and process metrics. Select each check's source explicitly. Do not substitute a request counter for scrape availability, delivery loss, retention or an SLI. The ordinary adapter byte/sample/record limits still apply: choose a window, resolution and profile split that can capture the required observations without truncation. Increasing the number of profiles does not relax evidence bounds or extend collection deadlines.

The managed demo allows 3–20 sources covering all three backends and exactly one complete journey trace. Every profile for the same backend must use the same scope and artifact/plan label mappings; the target emits a single consistent identity. The fixture's reviewed 76 applicable / 44 probe-backed exclusions remain unchanged. Generic standalone certification continues to support its own larger policy source bound; the engineering pilot rejects more than 20 selected profiles explicitly.

## Start explicitly and interpret the result

Start the telemetry stack, PostgreSQL/Temporal/evidence services and dedicated lifecycle, fixture, collector and verifier workers using the existing [local telemetry](local-telemetry.md), [fixture](fixture-agents.md), [scheduling](telemetry-scheduling.md) and [certification](certification.md) commands, pointing them at the generated files. Re-run the handoff/preflight after installing the new engineering paths. Only the existing explicit engineering start command begins execution.

The engineering parent runs four stages, freezes the artifact/plan/window, runs the managed fixture and independent assessment, collects every selected source through durable child workflows, binds actual authenticated evidence, and invokes the isolated 120-check verifier. The final gate rereads the certificate and current context before review readiness. Missing, empty or partial collections block readiness; if collection itself is incomplete the parent may block before invoking the verifier. Nonempty collections with missing assertion fields produce a full report with non-passing checks. A report with 120 entries is not necessarily a passing certificate.

The real-backend acceptance test exercises this entire parent/child chain, including a separate histogram query, authenticated evidence uploads and the actual collector/verifier subprocesses. It replays the parent history and rejects artifact/plan/preview/generation substitutions. Version 0.23 supplies [measured instrumentation and an explicit demo policy option](demo-instrumentation.md); its real-backend acceptance requires 76 applicable checks to pass and 44 probe-backed exclusions. Missing measurements, incorrect SLIs, stale samples and identity substitutions still block certification.

Live service certification still requires meaningful reviewed source mappings, complete instrumentation and independent expected values. Jira/model calls in these tests are synthetic. This increment adds no merge, deployment, promotion, production approval or cloud executor.
