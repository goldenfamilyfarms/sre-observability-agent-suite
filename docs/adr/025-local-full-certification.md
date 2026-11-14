# ADR 025: Consistent local-stack configuration for the complete certification workflow

Status: Accepted for the source-candidate engineering pilot.

## Context

The local backend stack proved scraping and query persistence, but its narrow journey query policy was not a 120-check verification policy. Engineering collection also restricted a run to one profile per backend with fixed 15-second/100-record requests, preventing independent histogram and other metric sources from matching the verifier's reviewed policy.

## Decision

Add an offline, atomic `local_telemetry prepare-certification` command. Require a complete reviewed managed-demo policy and matching local stack scope/target/certificate. Rebind transport origins and credential variable names only, preserve every assertion and independent expected value, and regenerate all downstream dispatch fingerprints together. Export separate role policies plus engineering configuration paths without credentials. Never start execution implicitly.

Allow up to 20 unique engineering profiles, including repeated backends, with explicitly bound request resolution/limit. Default inputs retain their existing fingerprint. Verify exact collection/verification source equality. Allow multiple demo queries only when their per-backend scope/artifact label mappings agree and exactly one full journey trace remains selected.

Use a real engineering parent, fixture/lifecycle workflows, Prometheus/Loki/Tempo, scheduled collector subprocesses, independent assessment and isolated verifier in acceptance tests. Evaluate all 120 slots from actual captured evidence; keep missing instrumentation non-passing and preserve current-generation and exact-artifact/plan validation. Existing incomplete-collection gates remain unchanged.

## Consequences

The wiring is implemented independently of whether a particular service earns a passing certificate. The demo's uninstrumented concerns remain visible gaps, not invented data or new applicability exemptions. Up to 20 parallel bounded queries increase resource demand; request budgets and evidence size limits still apply. Credentialed service acceptance and deployment provenance remain separate. See [the operator guide](../local-certification.md).
