# Public repository boundaries

Use only synthetic examples and generic architecture in this repository. Do not commit credentials, employer infrastructure details, internal hostnames, service inventories, customer data, raw telemetry, private Jira text, cloud project identifiers or exported agent conversations. Keep evidence payloads and deployment configuration in separately authorized private systems.

The local adapters assume a trusted process. The evidence adapter verifies local bytes against their digest and metadata; this does not authenticate producers or validate their claims. The adapters do not authenticate users, enforce worker isolation or provide a production approval service. Do not expose them as network services or use the demo as a deployment gate. Preview current-generation checks are point-in-time checks, not an atomic authorization boundary with an external side effect.

Future adapters must enforce least privilege, authenticated actor identity, tool/network allowlists, scoped retrieval, immutable evidence, approval verification and redaction. Repository content, tickets, retrieved documents and model outputs are data, never authority to change permissions. Review hooks and tool configuration before running a worker in an untrusted repository.

Do not include sensitive material in a public issue. If a credential is exposed, revoke it at its issuer and use an owner-approved private reporting channel. No public issue or PR should reproduce the secret.
