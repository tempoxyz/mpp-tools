from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

from flow_runner import compare_results, main, perform_request


class PerformRequestTests(unittest.TestCase):
    def test_put_and_patch_send_configured_body(self) -> None:
        for method in ("PUT", "PATCH"):
            with self.subTest(method=method):
                response = MagicMock()
                response.__enter__.return_value.status = 200
                response.__enter__.return_value.headers = {}
                response.__enter__.return_value.read.return_value = b""

                with patch("flow_runner.urllib.request.urlopen", return_value=response) as urlopen:
                    perform_request(
                        "https://example.com/paid",
                        {"http_method": method, "body": '{"amount":"1"}'},
                    )

                request = urlopen.call_args.args[0]
                self.assertEqual(request.data, b'{"amount":"1"}')
                self.assertEqual(request.get_header("Content-type"), "application/json")


class CompareResultsCapabilitySkipTests(unittest.TestCase):
    def test_skipping_a_required_capability_fails_not_passes(self) -> None:
        # HARNESS_SPEC.md documents http.payment_request as "Required for flow
        # conformance". An adapter that skips a case because it lacks that
        # capability has a real conformance gap and must not be marked passed,
        # even though the golden reference ran the case for real.
        golden = [
            {
                "name": "plain_payment_request",
                "outcome": {"ok": True, "status": 200},
            }
        ]
        actual = [
            {
                "name": "plain_payment_request",
                "outcome": {
                    "ok": True,
                    "status": 0,
                    "skipped": True,
                    "requires": "http.payment_request",
                },
            }
        ]

        results = compare_results(golden, actual, "broken-adapter")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("http.payment_request", results[0].error or "")

    def test_both_golden_and_actual_skipped_is_unaffected(self) -> None:
        outcome = {"ok": True, "status": 0, "skipped": True, "requires": "http.payment_request"}
        golden = [{"name": "plain_payment_request", "outcome": outcome}]
        actual = [{"name": "plain_payment_request", "outcome": dict(outcome)}]

        results = compare_results(golden, actual, "adapter")

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)


class FlowRunnerOutputTests(unittest.TestCase):
    def test_json_output_reports_early_data_validation_failure(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["flow_runner.py", "--output", "json"]),
            patch("flow_runner.load_flow_cases", side_effect=ValueError("duplicate path")),
            patch("flow_runner.start_server") as start_server,
            contextlib.redirect_stdout(stdout),
        ):
            status = main()

        self.assertEqual(status, 1)
        self.assertFalse(start_server.called)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "fail")
        self.assertEqual(output["failed"], 1)
        self.assertIn("duplicate path", output["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
