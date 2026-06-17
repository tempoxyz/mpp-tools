#!/usr/bin/env python3
"""Tests for vector runner helpers."""

from __future__ import annotations

import unittest

from harness import AdapterConfig
from vector_runner import VectorRunner


class VectorRunnerHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = VectorRunner(output_format="json")
        self.adapter = AdapterConfig(name="python", command=["python"], capabilities=[])

    def test_duration_limit_prefers_adapter_specific_value(self) -> None:
        scenario = {
            "maxDurationMs": 10000,
            "maxDurationMsByAdapter": {
                "python": 5000,
            },
        }

        self.assertEqual(self.runner.duration_limit_ms(scenario, self.adapter), 5000)

    def test_command_timeout_leaves_room_for_reporting_duration_failure(self) -> None:
        self.assertEqual(self.runner.command_timeout_seconds(None), 30.0)
        self.assertEqual(self.runner.command_timeout_seconds(5000), 6.0)
        self.assertEqual(self.runner.command_timeout_seconds(100), 1.1)

    def test_compare_duration_reports_budget_exceeded(self) -> None:
        passed, error = self.runner.compare_duration(5000, 5000.1)

        self.assertFalse(passed)
        self.assertEqual(error, "duration exceeded: expected <= 5000 ms, got 5000.1 ms")

    def test_compare_adapter_response_checks_error_message_substring(self) -> None:
        expected = {
            "ok": False,
            "error": {
                "type": "verification_error",
                "messageContains": "sponsor policy",
            },
        }
        actual = {
            "ok": False,
            "error": {
                "type": "verification_error",
                "message": "Invalid transaction: gas limit exceeds sponsor policy",
            },
        }

        passed, error = self.runner.compare_adapter_response(expected, actual)

        self.assertTrue(passed)
        self.assertIsNone(error)

    def test_compare_adapter_response_rejects_wrong_error_message(self) -> None:
        expected = {
            "ok": False,
            "error": {
                "type": "verification_error",
                "messageContains": "access list",
            },
        }
        actual = {
            "ok": False,
            "error": {
                "type": "verification_error",
                "message": "Invalid transaction: gas limit exceeds sponsor policy",
            },
        }

        passed, error = self.runner.compare_adapter_response(expected, actual)

        self.assertFalse(passed)
        self.assertEqual(
            error,
            "error.message mismatch: expected to contain 'access list', "
            "got 'Invalid transaction: gas limit exceeds sponsor policy'",
        )

    def test_scenario_wire_expands_repeat_shorthand(self) -> None:
        scenario = {
            "wire": {
                "prefix": "a",
                "repeat": "bc",
                "count": 3,
                "suffix": "d",
            },
        }

        self.assertEqual(self.runner.scenario_wire(scenario), "abcbcbcd")


if __name__ == "__main__":
    unittest.main()
