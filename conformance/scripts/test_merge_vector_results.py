#!/usr/bin/env python3
"""Tests for merging per-adapter vector result artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("merge_vector_results.py")


class MergeVectorResultsTest(unittest.TestCase):
    def merge(self, adapters: str, artifacts: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            result_dir = Path(tmp) / "vector-results"
            result_dir.mkdir()
            for adapter, payload in artifacts.items():
                (result_dir / f"results-{adapter}.json").write_text(
                    payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8",
                )
            output_path = Path(tmp) / "merged.json"
            env = os.environ.copy()
            env.pop("GITHUB_OUTPUT", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--adapters",
                    adapters,
                    "--result-dir",
                    str(result_dir),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(output_path.read_text(encoding="utf-8"))

    def test_healthy_artifacts_merge_to_pass(self) -> None:
        merged = self.merge(
            "go,python",
            {
                "go": {"status": "pass", "num_checks": 5, "passed": 5, "failed": 0, "errors": []},
                "python": {"status": "pass", "num_checks": 7, "passed": 7, "failed": 0, "errors": []},
            },
        )

        self.assertEqual(merged["status"], "pass")
        self.assertEqual(merged["num_checks"], 12)
        self.assertEqual(merged["failed"], 0)

    def test_empty_json_object_artifact_fails(self) -> None:
        merged = self.merge("go", {"go": {}})

        self.assertEqual(merged["status"], "fail")
        self.assertEqual(merged["failed"], 1)
        self.assertIn("zero conformance checks", merged["errors"][0]["error"])

    def test_zero_check_artifact_fails(self) -> None:
        merged = self.merge(
            "go",
            {"go": {"status": "pass", "num_checks": 0, "passed": 0, "failed": 0, "errors": []}},
        )

        self.assertEqual(merged["status"], "fail")

    def test_status_fail_without_failed_counts_fails(self) -> None:
        merged = self.merge(
            "go",
            {"go": {"status": "fail", "num_checks": 3, "passed": 3, "failed": 0, "errors": []}},
        )

        self.assertEqual(merged["status"], "fail")
        self.assertIn("status 'fail'", merged["errors"][0]["error"])

    def test_non_object_artifact_fails(self) -> None:
        merged = self.merge("go", {"go": "[]"})

        self.assertEqual(merged["status"], "fail")
        self.assertEqual(merged["failed"], 1)

    def test_missing_artifact_fails(self) -> None:
        merged = self.merge("go,missing", {"go": {"status": "pass", "num_checks": 2, "passed": 2, "failed": 0}})

        self.assertEqual(merged["status"], "fail")
        self.assertEqual(merged["num_checks"], 3)

    def test_no_adapters_fails(self) -> None:
        merged = self.merge("", {})

        self.assertEqual(merged["status"], "fail")
        self.assertIn("No adapters", merged["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
