#!/usr/bin/env python3
"""Tests for per-case error containment in the flow runner."""

from __future__ import annotations

import unittest

import flow_runner
from harness import AdapterClient, AdapterConfig


class RunFlowCaseSafelyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = AdapterClient(AdapterConfig(name="go", command=["true"], capabilities=[]))
        self.flow_case = {"name": "charge-success", "path": "/charge/success"}

    def test_exception_becomes_per_case_failure(self) -> None:
        original = flow_runner.run_flow_case

        def explode(*args: object, **kwargs: object) -> dict[str, object]:
            raise ValueError("go challenge.parse value failed schema validation")

        flow_runner.run_flow_case = explode
        try:
            result = flow_runner.run_flow_case_safely(
                self.client, "http://127.0.0.1:1", self.flow_case, {}, verbose=False
            )
        finally:
            flow_runner.run_flow_case = original

        self.assertEqual(result["name"], "charge-success")
        self.assertFalse(result["outcome"]["ok"])
        self.assertIn("case_error", result["outcome"]["error_type"])
        self.assertIn("schema validation", result["outcome"]["error_type"])

    def test_successful_case_passes_through_unchanged(self) -> None:
        original = flow_runner.run_flow_case
        expected = {"name": "charge-success", "outcome": {"ok": True, "status": 200}}
        flow_runner.run_flow_case = lambda *args, **kwargs: expected
        try:
            result = flow_runner.run_flow_case_safely(
                self.client, "http://127.0.0.1:1", self.flow_case, {}, verbose=False
            )
        finally:
            flow_runner.run_flow_case = original

        self.assertIs(result, expected)


if __name__ == "__main__":
    unittest.main()
