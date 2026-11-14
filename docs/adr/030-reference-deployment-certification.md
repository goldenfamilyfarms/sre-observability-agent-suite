# ADR 030: Bound reference deployment resources and honest image certification

Status: Accepted for implementation; private infrastructure acceptance pending.

The Python reference needs persistent outbox state, private HTTPS access and authenticated scraping. Its image-build smoke test alone does not establish the complete certification path.

Add an optional typed reference profile to the existing approved delivery target. Render PVC, Ingress and ServiceMonitor alongside the exact Deployment/Service. Include all profile fields in the existing binding, use a release-specific claim and reject changes to owned readiness resources. Keep the generic delivery target unchanged when the profile is absent.

Exercise the actual built image, registry transport, replacement storage, independent fixtures, real telemetry backends and 120-check verifier in one disposable integration test. Model cloud control-plane responses explicitly rather than claiming a cloud deployment. Unmeasured checks remain inconclusive and block the gate; the older demo's exclusions and passing results cannot transfer to this target.

The tradeoffs are one reference replica, CSI/Operator/controller prerequisites and internal HTTP scraping. Private operators retain responsibility for network encryption policy, ingress exposure, secrets, storage retention and live cloud acceptance. No preparation command grants authority or starts a deployment.
