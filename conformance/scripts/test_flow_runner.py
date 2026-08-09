from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

from flow_runner import compare_results, main, perform_request, run_client_http_flow_case


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


class ClientHttpFlowTests(unittest.TestCase):
    def test_paid_post_requires_the_adapter_to_preserve_the_body(self) -> None:
        body = '{"prompt":"expensive question"}'
        flow_case = {
            "name": "same_origin_post_payment_request",
            "path": "/charge/same-origin-post-payment-request",
            "http_method": "POST",
            "body": body,
            "verify_body_preserved": True,
        }
        client = MagicMock()
        client.adapter.capabilities = ["http.payment_request"]
        client.call.return_value = {
            "ok": True,
            "value": {
                "status": 200,
                "headers": {},
                "body": json.dumps(
                    {
                        "name": "same_origin_post_payment_request",
                        "received_body": body,
                    }
                ),
            },
        }

        result = run_client_http_flow_case(client, "http://127.0.0.1:3000", flow_case, False)

        self.assertTrue(result["outcome"]["ok"])
        self.assertTrue(result["body_preserved"])

        client.call.return_value["value"]["body"] = json.dumps(
            {
                "name": "same_origin_post_payment_request",
                "received_body": "",
            }
        )
        result = run_client_http_flow_case(client, "http://127.0.0.1:3000", flow_case, False)

        self.assertFalse(result["outcome"]["ok"])
        self.assertFalse(result["body_preserved"])


class CompareResultsCapabilitySkipTests(unittest.TestCase):
    def test_missing_http_payment_request_is_temporarily_non_blocking(self) -> None:
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
        self.assertTrue(results[0].passed)

    def test_skipping_another_required_capability_still_fails(self) -> None:
        golden = [{"name": "required_case", "outcome": {"ok": True, "status": 200}}]
        actual = [
            {
                "name": "required_case",
                "outcome": {
                    "ok": True,
                    "status": 0,
                    "skipped": True,
                    "requires": "another.required_capability",
                },
            }
        ]

        results = compare_results(golden, actual, "broken-adapter")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("another.required_capability", results[0].error or "")

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
