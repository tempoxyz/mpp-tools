#!/usr/bin/env python3
"""Tests for referenced conformance PR validation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("validate_conformance_pr.py")
WORKFLOW = SCRIPT.parents[2] / ".github" / "workflows" / "sdk-conformance-policy.yml"


class ValidateConformancePrTest(unittest.TestCase):
    def run_validator(self, files: list[str]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pr_path = tmp_path / "pr.json"
            files_path = tmp_path / "files.txt"
            pr_path.write_text(
                json.dumps({"state": "open", "merged_at": None}),
                encoding="utf-8",
            )
            files_path.write_text("\n".join(files), encoding="utf-8")

            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--pr-json",
                    str(pr_path),
                    "--files",
                    str(files_path),
                    "--conformance-paths",
                    "conformance/adapters/**\nconformance/flows/**",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_accepts_adapter_implementation_as_conformance_coverage(self) -> None:
        result = self.run_validator(["conformance/adapters/python/adapter.py"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("conformance/adapters/python/adapter.py", result.stdout)

    def test_rejects_unrelated_changes(self) -> None:
        result = self.run_validator(["README.md"])

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("does not touch", result.stdout)

    def test_workflow_default_includes_adapter_coverage(self) -> None:
        self.assertIn("conformance/adapters/**", WORKFLOW.read_text(encoding="utf-8"))



class PathMatchesTest(unittest.TestCase):
    def test_trailing_slash_pattern_matches_files_below(self) -> None:
        from validate_conformance_pr import path_matches

        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/"))
        self.assertTrue(path_matches("/conformance/adapters/python/foo.py", "/conformance/adapters/"))
        self.assertFalse(path_matches("conformance/adapters-old/foo.py", "conformance/adapters/"))
        self.assertFalse(path_matches("unrelated/foo.py", "conformance/adapters/"))

    def test_trailing_slash_glob_pattern_matches_files_below(self) -> None:
        from validate_conformance_pr import path_matches

        self.assertTrue(path_matches("src/pkg/file.py", "src/*/"))
        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/py*/"))
        self.assertFalse(path_matches("conformance/adapters/go/foo.go", "conformance/adapters/py*/"))
        self.assertFalse(path_matches("lib/pkg/file.py", "src/*/"))

    def test_other_pattern_forms_unchanged(self) -> None:
        from validate_conformance_pr import path_matches

        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/**"))
        self.assertTrue(path_matches("conformance/adapters", "conformance/adapters/**"))
        self.assertTrue(path_matches("pkg/mpp/parse.go", "pkg/*/parse.go"))
        self.assertFalse(path_matches("pkg/mpp/parse.go", "pkg/server/*"))


if __name__ == "__main__":
    unittest.main()
