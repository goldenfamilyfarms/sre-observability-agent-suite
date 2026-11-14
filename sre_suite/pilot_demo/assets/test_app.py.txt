"""Regression tests that must keep passing before and after the pilot change."""

import json
import unittest

from app import handle_request


class RegressionTests(unittest.TestCase):
    def test_health_response_and_log(self):
        status, body, line = handle_request("GET", "/health")
        self.assertEqual((status, body), (200, "ok"))
        event = json.loads(line)
        self.assertEqual(event["event"], "request_completed")
        self.assertEqual(event["route"], "/health")
        self.assertEqual(event["status"], status)
        self.assertNotIn("\n", line)

    def test_unknown_route_never_logs_raw_path(self):
        status, body, line = handle_request("GET", "/private?token=synthetic-secret")
        self.assertEqual((status, body), (404, "not found"))
        self.assertEqual(json.loads(line)["route"], "unknown")
        self.assertNotIn("synthetic-secret", line)
        self.assertNotIn("token", line)

    def test_unsupported_method_is_bounded(self):
        status, body, line = handle_request("POST\nsynthetic-secret", "/health")
        self.assertEqual((status, body), (405, "method not allowed"))
        self.assertEqual(json.loads(line)["route"], "unsupported")
        self.assertNotIn("synthetic-secret", line)


if __name__ == "__main__":
    unittest.main()
