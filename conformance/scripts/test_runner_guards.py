#!/usr/bin/env python3
"""Tests for the zero-check guards shared by the flow and server-verify runners."""

from __future__ import annotations

import unittest

import flow_runner
import server_verify_runner


class FlowRunnerGuardTest(unittest.TestCase):
    def test_empty_results_become_a_failure(self) -> None:
        results: list[flow_runner.RunResult] = []

        flow_runner.guard_no_results(results, "all")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("No flow conformance checks", results[0].error or "")

    def test_existing_results_are_untouched(self) -> None:
        results = [flow_runner.RunResult(adapter="go", name="free-endpoint", passed=True)]

        flow_runner.guard_no_results(results, "all")

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)


class ServerVerifyRunnerGuardTest(unittest.TestCase):
    def test_empty_results_become_a_failure(self) -> None:
        results: list[server_verify_runner.RunResult] = []

        server_verify_runner.guard_no_results(results, "all")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("No server verification checks", results[0].error or "")

    def test_existing_failure_is_not_duplicated(self) -> None:
        results = [server_verify_runner.RunResult(adapter="go", name="case", passed=False, error="boom")]

        server_verify_runner.guard_no_results(results, "all")

        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
