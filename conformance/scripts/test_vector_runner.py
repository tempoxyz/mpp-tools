#!/usr/bin/env python3
"""Tests for vector runner helpers."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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


class VectorRunnerJsonArtifactTest(unittest.TestCase):
    """The json output format must stay machine-readable on stdout."""

    def setUp(self) -> None:
        self.runner = VectorRunner(output_format="json")
        self.adapter = AdapterConfig(name="python", command=["python"], capabilities=[])

    def run_vector_file_capturing_stdout(self, content: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            vector_path = Path(tmp) / "sample.json"
            vector_path.write_text(content, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.runner.run_vector_file(self.adapter, vector_path)
            return stdout.getvalue()

    def test_vector_without_commands_does_not_write_to_stdout(self) -> None:
        printed = self.run_vector_file_capturing_stdout(json.dumps({"scenarios": []}))

        self.assertEqual(printed, "")

    def test_missing_vector_file_does_not_write_to_stdout(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.runner.run_vector_file(self.adapter, Path("/nonexistent/vectors/missing.json"))

        self.assertEqual(stdout.getvalue(), "")

    def test_malformed_vector_file_is_reported_not_fatal(self) -> None:
        import vector_runner as vector_runner_module

        fake_adapter = AdapterConfig(name="fake", command=["true"], capabilities=[])
        with tempfile.TemporaryDirectory() as tmp:
            vectors_dir = Path(tmp)
            (vectors_dir / "broken.json").write_text("{not json", encoding="utf-8")

            original_discover_vectors = vector_runner_module.discover_vector_files
            original_discover_adapters = vector_runner_module.discover_adapters
            vector_runner_module.discover_vector_files = lambda: {"broken": vectors_dir / "broken.json"}
            vector_runner_module.discover_adapters = lambda: {"fake": fake_adapter}
            try:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    success = self.runner.run(adapter_names=["fake"], vector_names=["broken"])
            finally:
                vector_runner_module.discover_vector_files = original_discover_vectors
                vector_runner_module.discover_adapters = original_discover_adapters

        self.assertFalse(success)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "fail")
        failing = [check for check in output["checks"] if check["status"] == "FAILURE"]
        self.assertTrue(any("vector-file-error" in check["id"] for check in failing))


if __name__ == "__main__":
    unittest.main()
