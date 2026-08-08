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
from vector_runner import VectorRunner, expects_failure


class VectorRunnerHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolved_adapters: list[str] = []

        def resolve_version(adapter: str) -> str:
            self.resolved_adapters.append(adapter)
            return "0.9.1"

        self.runner = VectorRunner(
            output_format="json", sdk_version_resolver=resolve_version
        )
        self.adapter = AdapterConfig(name="python", command=["python"], capabilities=[])

    def test_duration_limit_prefers_adapter_specific_value(self) -> None:
        scenario = {
            "maxDurationMs": 10000,
            "maxDurationMsByAdapter": {
                "python": 5000,
            },
        }

        self.assertEqual(self.runner.duration_limit_ms(scenario, self.adapter), 5000)

    def test_expected_failure_detection(self) -> None:
        cases = [
            ({"success": False}, "success", True),
            ({"success": True}, "success", False),
            ({"ok": False}, "ok", True),
            (True, "success", False),
        ]

        for expectation, result_key, expected in cases:
            with self.subTest(expectation=expectation, result_key=result_key):
                self.assertEqual(
                    expects_failure(expectation, result_key),
                    expected,
                )

    def test_command_timeout_leaves_room_for_reporting_duration_failure(self) -> None:
        self.assertEqual(self.runner.command_timeout_seconds(None), 30.0)
        self.assertEqual(self.runner.command_timeout_seconds(5000), 6.0)
        self.assertEqual(self.runner.command_timeout_seconds(100), 1.1)

    def test_compare_duration_reports_budget_exceeded(self) -> None:
        passed, error = self.runner.compare_duration(5000, 5000.1)

        self.assertFalse(passed)
        self.assertEqual(error, "duration exceeded: expected <= 5000 ms, got 5000.1 ms")

    def test_scenario_version_constraint_applies_when_matching(self) -> None:
        scenario = {"sdkVersions": {"python": ">0.9.0 <1.0.0"}}

        applies, reason = self.runner.scenario_version_applies(scenario, self.adapter)

        self.assertTrue(applies)
        self.assertIsNone(reason)
        self.assertEqual(self.resolved_adapters, ["python"])

    def test_scenario_version_constraint_skips_when_not_matching(self) -> None:
        scenario = {"sdkVersions": {"python": ">0.9.1"}}

        applies, reason = self.runner.scenario_version_applies(scenario, self.adapter)

        self.assertFalse(applies)
        self.assertEqual(reason, "python@0.9.1 does not satisfy >0.9.1")

    def test_scenario_version_constraint_ignores_unspecified_adapter(self) -> None:
        scenario = {"sdkVersions": {"rust": ">=0.12.0"}}

        applies, reason = self.runner.scenario_version_applies(scenario, self.adapter)

        self.assertTrue(applies)
        self.assertIsNone(reason)
        self.assertEqual(self.resolved_adapters, [])

    def test_scenario_version_resolver_is_cached_per_adapter(self) -> None:
        scenario = {"sdkVersions": {"python": ">=0.9.0"}}

        self.runner.scenario_version_applies(scenario, self.adapter)
        self.runner.scenario_version_applies(scenario, self.adapter)

        self.assertEqual(self.resolved_adapters, ["python"])

    def test_scenario_version_constraints_require_an_object(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "sdkVersions must be an object keyed by adapter name"
        ):
            self.runner.scenario_version_applies(
                {"sdkVersions": ">=0.9.0"}, self.adapter
            )

    def test_scenario_version_constraints_reject_unknown_adapter(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "sdkVersions contains unknown adapter keys: pyhton"
        ):
            self.runner.scenario_version_applies(
                {"sdkVersions": {"pyhton": ">=0.9.0"}}, self.adapter
            )

    def test_scenario_version_constraint_requires_a_string(self) -> None:
        with self.assertRaisesRegex(ValueError, "sdkVersions.python must be a string"):
            self.runner.scenario_version_applies(
                {"sdkVersions": {"python": 0.9}}, self.adapter
            )

    def test_scenario_version_constraints_validate_every_adapter_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "sdkVersions.rust must be a string"):
            self.runner.scenario_version_applies(
                {"sdkVersions": {"rust": 0.12}}, self.adapter
            )

    def test_scenario_adapters_reject_unknown_name(self) -> None:
        self.runner.known_adapter_names = {"python", "rust"}

        with self.assertRaisesRegex(ValueError, "adapters contains unknown names: pyhton"):
            self.runner.scenario_adapters({"adapters": ["pyhton"]})

    def test_scenario_adapters_accept_registered_names(self) -> None:
        self.runner.known_adapter_names = {"python", "rust"}

        self.assertEqual(
            self.runner.scenario_adapters({"adapters": ["python"]}),
            ["python"],
        )

    def test_run_vector_file_skips_nonmatching_sdk_version(self) -> None:
        vector = {
            "version": "2.0.0",
            "description": "SDK version filtering fixture",
            "spec_ref": "test",
            "commands": {"parse": "parse-www-authenticate"},
            "scenarios": [
                {
                    "name": "future_rule",
                    "description": "Only applies to a future SDK version",
                    "tags": ["version"],
                    "object": {},
                    "wire": "unused",
                    "tests": {"parse": True},
                    "sdkVersions": {"python": ">0.9.1"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            vector_path = Path(tmp) / "sample.json"
            vector_path.write_text(json.dumps(vector), encoding="utf-8")

            self.runner.run_vector_file(self.adapter, vector_path)

        self.assertEqual(self.runner.results, [])
        self.assertEqual(self.runner.version_skips, 1)

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

    def test_invalid_vector_does_not_write_to_stdout(self) -> None:
        stdout = io.StringIO()
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            with contextlib.redirect_stdout(stdout):
                self.run_vector_file_capturing_stdout(json.dumps({"scenarios": []}))

        self.assertEqual(stdout.getvalue(), "")

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

    def test_all_version_skipped_selection_fails(self) -> None:
        import vector_runner as vector_runner_module

        fake_adapter = AdapterConfig(name="fake", command=["true"], capabilities=[])
        vector = {
            "version": "2.0.0",
            "description": "SDK version filtering fixture",
            "spec_ref": "test",
            "commands": {"parse": "parse-www-authenticate"},
            "scenarios": [
                {
                    "name": "future_rule",
                    "description": "Only applies to a future SDK version",
                    "tags": ["version"],
                    "object": {},
                    "wire": "unused",
                    "tests": {"parse": True},
                    "sdkVersions": {"fake": ">0.9.1"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            vector_path = Path(tmp) / "future.json"
            vector_path.write_text(json.dumps(vector), encoding="utf-8")
            original_discover_vectors = vector_runner_module.discover_vector_files
            original_discover_adapters = vector_runner_module.discover_adapters
            vector_runner_module.discover_vector_files = lambda: {"future": vector_path}
            vector_runner_module.discover_adapters = lambda: {"fake": fake_adapter}
            runner = VectorRunner(
                output_format="json", sdk_version_resolver=lambda _: "0.9.1"
            )
            try:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    success = runner.run(
                        adapter_names=["fake"], vector_names=["future"]
                    )
            finally:
                vector_runner_module.discover_vector_files = original_discover_vectors
                vector_runner_module.discover_adapters = original_discover_adapters

        self.assertFalse(success)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "fail")
        self.assertEqual(output["num_checks"], 1)
        self.assertEqual(output["skipped"], 1)
        failing = [c for c in output["checks"] if c["status"] == "FAILURE"]
        self.assertEqual(len(failing), 1)
        self.assertIn("version-skipped", failing[0]["error"])


if __name__ == "__main__":
    unittest.main()
