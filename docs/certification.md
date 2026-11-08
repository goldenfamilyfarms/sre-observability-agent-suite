# Certification boundary

The packaged `sre_suite/catalog.json` contains exactly 40 trace, 40 log and 40 PromQL slots with stable IDs and version `0.1.0-draft`. Each slot identifies a distinct concern, applicability work, fixture, expected result, timeout, mandatory flag, severity and evidence requirements. Query templates and assertions are null because no service, telemetry source, schema or threshold has been verified.

This is a design inventory, not 120 implemented tests. The code tests the report rules, not the telemetry checks. Synthetic ready definitions in unit tests are never exported as the real catalog.

## Moving a slot to executable status

1. Discover the authorized datasource and service capability; record evidence for applicability.
2. Pin instrumentation/semantic-convention versions and map actual telemetry fields.
3. Define a bounded fixture and observation window tied to the deployed artifact.
4. Implement a reviewed TraceQL, LogQL or PromQL template with safe parameter handling and a deterministic assertion. Include required sample volume, tolerances and expected failure behavior.
5. Test positive, absent, malformed, stale, wrong-service and wrong-artifact results. Test query error, timeout and partial backend response.
6. Record evidence references and content digests; validate them through the trusted evidence service. Never commit raw private telemetry to this repository.
7. Version and review the catalog change. Repeat for every final-certification slot.

## Report rules implemented now

`CertificationReport` includes schema/catalog versions, exact release, window, individual results, verdict and reasons. All 120 IDs must appear exactly once; missing, unknown or duplicate IDs produce INCOMPLETE. All result versions/releases/windows and fresh evidence must match. Evidence observation must fall inside the report window.

Phase 1 advances the report schema to version 2: the release and every evidence/result scope must include the same environment ID and generation. Reports remain historical evaluations of their supplied scope; a controller must check the current generation before using one as a release gate. The aggregator does not query the preview registry or local evidence-content store itself.

PASS requires a deterministic assertion success. FAIL on an applicable mandatory check yields FAILED when the report is otherwise complete. ERROR, INCONCLUSIVE, missing evidence or invalid scope yields INCOMPLETE. If both failure and incomplete evidence exist, the verdict is INCOMPLETE and individual failures remain in the results. NOT_APPLICABLE requires justification and evidence; applicability decisions must come from a trusted assessor. Every slot, even one marked not applicable, must have a completed definition before the draft can certify anything.

The aggregator trusts typed inputs from adapters. It does not execute queries, authenticate a verifier, resolve evidence, evaluate arbitrary assertion code, enforce query deadlines or attest a deployed artifact. Those controls must be added before using PASSED as a shipping gate.
