"""Immutable pilot acceptance gate. Default mode must fail on the baseline.

--baseline-report succeeds only for the exact, expected missing-correlation gap.
Never use that mode as the worker's test command.
"""

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import handle_request
from test_app import RegressionTests


class CorrelationTests(unittest.TestCase):
    def test_valid_context_is_correlated(self):
        for trace in ("1" * 32, "0123456789abcdef" * 2, "0" * 31 + "f"):
            for method, path in (("GET", "/health"), ("GET", "/missing"), ("POST", "/health")):
                event = json.loads(handle_request(method, path, trace)[2])
                self.assertEqual(event.get("trace_id"), trace)

    def test_invalid_context_is_omitted(self):
        for trace in (None, "", "0" * 32, "a" * 31, "a" * 33, "A" * 32,
                      "g" * 32, "a" * 32 + "\n", "secret\nforged", 42, True, [], {}):
            event = json.loads(handle_request("GET", "/health", trace)[2])
            self.assertNotIn("trace_id", event)
            self.assertEqual(set(event), {"event", "route", "status"})

    def test_context_does_not_change_responses(self):
        for method, path in (("GET", "/health"), ("GET", "/unknown"), ("DELETE", "/health")):
            self.assertEqual(handle_request(method, path, "b" * 32)[:2], handle_request(method, path)[:2])

    def test_no_raw_context_or_extra_fields_in_log(self):
        event = json.loads(handle_request("GET", "/health", "c" * 32)[2])
        self.assertLessEqual(set(event), {"event", "route", "status", "trace_id"})
        self.assertNotIn("\n", handle_request("GET", "/health", "forged\nline")[2])


def run_report() -> dict[str, object]:
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(RegressionTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(CorrelationTests),
    ])
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    failures = sorted(test.id().split(".")[-1] for test, _ in result.failures)
    errors = sorted(test.id().split(".")[-1] for test, _ in result.errors)
    return {"tests_run": result.testsRun, "failures": failures, "errors": errors,
            "skipped": len(result.skipped), "passed": result.wasSuccessful()}


if __name__ == "__main__":
    report = run_report()
    print(json.dumps(report, sort_keys=True))
    if sys.argv[1:] == ["--baseline-report"]:
        expected = {"tests_run": 7, "failures": ["test_valid_context_is_correlated"],
                    "errors": [], "skipped": 0, "passed": False}
        sys.exit(0 if report == expected else 1)
    if sys.argv[1:]:
        sys.exit(2)
    sys.exit(0 if report["passed"] else 1)
