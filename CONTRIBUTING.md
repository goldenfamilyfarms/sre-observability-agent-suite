# Contributing

Use a feature branch and a reviewable pull request. Keep implementation, independent review, QA and shipping responsibilities separate. Do not merge or deploy without the relevant authorization. Add evidence and tests for behavioral changes, especially denial paths and interrupted external operations.

Run the commands in README before proposing a change. Document status accurately: implemented, tested locally, tested with mocks, verified with a real integration, deployed and production-ready are different claims. Update ADRs when changing architectural boundaries.

Public examples must be synthetic. Never copy private inventories, telemetry, credentials or internal project details into this repository. Keep dependencies pinned and review action updates before changing their immutable references.
