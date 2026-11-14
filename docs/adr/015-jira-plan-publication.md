# ADR-015: Scoped plan review and conservative Jira creation

Status: accepted for the bounded planning pilot; live Jira acceptance pending.

Plans use stable planned IDs, full preview scope, verified evidence and explicit dependency order. The operator reviews the complete private dry-run before selecting its destination-bound digest for opt-in publication. Created issues receive the existing typed execution contract after Jira assigns their real keys; dependencies resolve to those keys. Multiline Jira fields carry plain ADF text, with bounded decoding and rejection of rich content.

Issue creation is non-idempotent. Persist an intent on a private shared journal before each POST, serialize publishers with a file lock, and attach a deterministic label and fingerprint property. After uncertainty, reconcile a matching issue or stop. Never infer permission to recreate from an empty, eventually consistent search result. Verify identity and existing contract content before replaying fixed-value edits. Recheck preview generation and evidence at each boundary; retain partial external effects for inspection if a later fence fails.

This avoids claiming an exactly-once Jira transaction. It requires durable retained state and one shared locking domain, and can stop even when a crash happened before the request was sent. Independent journals, lost state, concurrent third-party changes and Jira automation are outside this guarantee. The adapter neither deletes partial issues nor starts or accepts engineering work. Production approval policy is unchanged. Temporal remains the execution authority; the journal tracks this bounded publication operation, not canonical autonomous workflow state.

See the [planning guide](../jira-planning.md) for configuration, recovery limits and verification. A remote database/outbox and authenticated planning approval service remain later operational work.
