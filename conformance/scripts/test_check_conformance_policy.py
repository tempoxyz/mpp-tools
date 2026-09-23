#!/usr/bin/env python3
"""Tests for SDK conformance policy checks."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check_conformance_policy.py")

# Modules that each carry a copy of path_matches and must stay in sync.
PATH_MATCH_MODULES = [
    "check_conformance_policy",
    "validate_conformance_pr",
    "select_ci_adapters",
]


class PathMatchesTest(unittest.TestCase):
    def each_path_matches(self):
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            for module_name in PATH_MATCH_MODULES:
                module = importlib.import_module(module_name)
                yield module_name, module.path_matches
        finally:
            sys.path.remove(str(SCRIPT.parent))

    def test_directory_pattern_matches_files_under_directory(self) -> None:
        for module_name, path_matches in self.each_path_matches():
            with self.subTest(module=module_name):
                self.assertTrue(path_matches("src/proto/wire.go", "src/proto/"))
                self.assertTrue(path_matches("src/proto/nested/deep.rs", "src/proto/"))
                self.assertTrue(path_matches("/src/proto/wire.go", "src/proto/"))

    def test_directory_pattern_does_not_overmatch(self) -> None:
        for module_name, path_matches in self.each_path_matches():
            with self.subTest(module=module_name):
                self.assertFalse(path_matches("src/protocol/wire.go", "src/proto/"))
                self.assertFalse(path_matches("src/proto", "src/proto/"))
                self.assertFalse(path_matches("other/src/proto/wire.go", "src/proto/"))

    def test_recursive_glob_pattern_still_matches(self) -> None:
        for module_name, path_matches in self.each_path_matches():
            with self.subTest(module=module_name):
                self.assertTrue(path_matches("conformance/vectors/receipt.json", "conformance/vectors/**"))
                self.assertTrue(path_matches("conformance/vectors", "conformance/vectors/**"))
                self.assertFalse(path_matches("conformance/schemas/x.json", "conformance/vectors/**"))

    def test_exact_and_glob_patterns_still_match(self) -> None:
        for module_name, path_matches in self.each_path_matches():
            with self.subTest(module=module_name):
                self.assertTrue(path_matches("conformance/operations.json", "conformance/operations.json"))
                self.assertTrue(path_matches("pkg/parse.go", "pkg/*.go"))
                self.assertFalse(path_matches("pkg/readme.md", "pkg/*.go"))
                self.assertFalse(path_matches("pkg/parse.go", ""))
                self.assertFalse(path_matches("pkg/parse.go", "/"))


class CheckConformancePolicyTest(unittest.TestCase):
    def run_policy(
        self,
        *,
        body: str = "",
        labels: list[str] | None = None,
        changed_files: list[str] | None = None,
        require_reference: str = "false",
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        labels = labels or []
        changed_files = changed_files or ["pkg/mpp/parse.go"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            event_path = tmp_path / "event.json"
            files_path = tmp_path / "changed-files.txt"
            output_path = tmp_path / "github-output.txt"

            event_path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "body": body,
                            "labels": [{"name": name} for name in labels],
                        }
                    }
                ),
                encoding="utf-8",
            )
            files_path.write_text("\n".join(changed_files), encoding="utf-8")

            env = os.environ.copy()
            env["GITHUB_OUTPUT"] = str(output_path)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--event-path",
                    str(event_path),
                    "--changed-files",
                    str(files_path),
                    "--protocol-paths",
                    "pkg/**",
                    "--require-reference",
                    require_reference,
                ],
                check=False,
                capture_output=True,
                env=env,
                text=True,
            )
            outputs = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            return result, outputs

    def test_protocol_change_uses_default_conformance_ref_without_reference(self) -> None:
        result, outputs = self.run_policy()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using default conformance ref 'main'", result.stdout)
        self.assertIn("conformance_ref=main", outputs)
        self.assertIn("protocol_changed=true", outputs)

    def test_protocol_change_can_require_reference(self) -> None:
        result, outputs = self.run_policy(require_reference="true")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("does not reference conformance coverage", result.stderr)
        self.assertIn("Conformance-PR: tempoxyz/mpp-tools#123", result.stderr)
        self.assertIn("protocol_changed=true", outputs)

    def test_conformance_pr_overrides_default_ref(self) -> None:
        result, outputs = self.run_policy(body="Conformance-PR: tempoxyz/mpp-tools#123")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Using mpp-tools PR #123 as conformance ref.", result.stdout)
        self.assertIn("conformance_ref=refs/pull/123/head", outputs)
        self.assertIn("conformance_pr=123", outputs)


if __name__ == "__main__":
    unittest.main()
