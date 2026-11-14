# SRE observability pilot demo

A deliberately small, synthetic service for one Jira-to-draft-PR engineering run. It opens no network listener, uses no external dependencies and contains no real telemetry or credentials. `handle_request` represents a request handler and returns a response plus one structured JSON log event.

The baseline handles requests and safely logs route/status, but deliberately omits request-context trace IDs. The pilot ticket asks the worker to change only `app.py`. `test_app.py`, `acceptance.py` and CI must remain unchanged.

## Baseline checks

```sh
python -m unittest test_app -v
python acceptance.py --baseline-report
```

Three regression tests pass. The report proves exactly one expected acceptance failure: valid request context is not correlated. That report is baseline evidence, not a successful candidate gate.

## Candidate gate

```sh
python acceptance.py
```

All seven tests must pass after the change. A valid context ID is a string containing exactly 32 lowercase hexadecimal characters, with at least one nonzero character. Preserve the exact ID for health, unknown-route and unsupported-method requests. Omit invalid/missing IDs, including all-zero IDs, uppercase IDs, wrong lengths, nonstrings and newlines. Responses, route sanitization and existing log fields must stay unchanged. Do not log raw context, arbitrary headers, request bodies or extra fields.

This is a synthetic context contract, not a full W3C traceparent parser or OpenTelemetry implementation. Real telemetry export and 120-check certification are separate work.

## Run through the agent suite

Use the preparation tool and [live-run checklist](https://github.com/goldenfamilyfarms/sre-observability-agent-suite/blob/feat/demo-live-pilot/docs/live-pilot-checklist.md) from the companion suite. Supply real Jira/GitHub/model configuration privately. The tool verifies the exact baseline source before generating timestamped evidence, a preview manifest and a non-executable ticket draft. After authenticated evidence upload, finalize the ticket contract using the server's returned metadata.

While this bootstrap PR is unmerged, use `feat/demo-baseline` as the configured publication base and its full current commit SHA. The bootstrap PR number anchors the preview identity; the worker will open a separate candidate draft PR. No merge or deployment is part of the pilot.
