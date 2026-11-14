# ADR-022: pinned demo fixture controller

Status: accepted for the local non-production pilot; private backend acceptance pending. Date: 2026-11-14.

Decision: add a completed, packaged demo service and a loopback HTTPS fixture controller separate from the intentionally incomplete engineering pilot. Match the exact source-candidate recipe against installed service bytes and independently pin the plan, scope, generation, window and fixture policy. Execute only fixed pure handler scenarios; never import worker code or accept executable/URL inputs.

Commit local results, measurements and telemetry outbox rows atomically in SQLite. Deduplicate exact commands using the existing stable fixture key; mismatched retries fail. Current context is required for new effects, cached responses, metrics and export. Provide separately authenticated scrape access and explicit OTLP/Loki outbox export. Local transaction recovery does not imply exactly-once downstream telemetry delivery.

Map all 120 catalog slots to reviewed probes. The pinned demo has 44 absent-capability exclusions and 76 applicable checks. Preserve infrastructure, retention, scrape and SLO gates. Require independently reviewed private queries and ten expected-value lists rather than manufacturing observations or claiming the sample service can prove backend configuration. The assessor and verifier remain separate agents; successful fixture execution cannot itself produce passing telemetry evidence.

Alternatives rejected: importing an arbitrary candidate into a credentialed controller, echoing unverified artifact metadata, repeating effects after lost acknowledgements, using a universal synthetic passing-record generator, and marking missing infrastructure evidence not applicable.

Consequences: this target covers only the named demo recipe and component. It is not a general workload launcher, public ingress, deployed-image attestation or cloud provisioner. The operator explicitly pins a future plan, starts the controller, arranges scraping/export before collection, and supplies private policy/oracles. Full controller lifecycle orchestration, target provisioning, cross-host failover and live backend acceptance remain separate work. See [operator guide](../demo-fixture-controller.md).
