# ADR 029: Independent image builds and deployed-target fixtures

Status: implemented; private registry, build-host and target acceptance pending.

Use a separately authenticated build worker to verify the full candidate tree against the four-stage engineering evidence chain. Build only explicitly listed immutable files with a generated recipe, a pinned base, non-root steps and disabled build-step networking. Keep registry publication credentials out of the build process. Validate the OCI archive and preserve its manifest digest through registry publication before issuing existing schema-2 image-build evidence.

Keep builds and publication explicit and durable. Retries reuse saved immutable bytes. Registry tags are transport aids; image digests are authoritative. Every phase rechecks context, generation, evidence and exact approval. Publishing an image is a suite checkpoint because delivery must authorize a newly known artifact digest. The suite attestation authenticates its producer; it is not a portable cryptographic signature.

Require independent build/readiness evidence and a live identity/binding contract for the new target-service fixture profile. Ship a standalone Python reference with real HTTP measurements, correlated traces/logs, metrics and a transactional export outbox. Keep reviewed applicability and expected values independent of service output. Preserve all 120 catalog slots without inventing capabilities or pass results. Persistent storage across pod replacement is an environment responsibility, distinct from process-level SQLite recovery.

See [operator guide](../image-build-and-target-fixtures.md). No workflow, registry publication, merge or deployment is enabled merely by importing the code or passing tests.
