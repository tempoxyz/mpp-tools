#!/usr/bin/env python3
"""Tests for SDK conformance policy checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check_conformance_policy.py")


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



class PathMatchesTest(unittest.TestCase):
    def test_trailing_slash_pattern_matches_files_below(self) -> None:
        from check_conformance_policy import path_matches

        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/"))
        self.assertTrue(path_matches("/conformance/adapters/python/foo.py", "/conformance/adapters/"))
        self.assertFalse(path_matches("conformance/adapters-old/foo.py", "conformance/adapters/"))
        self.assertFalse(path_matches("unrelated/foo.py", "conformance/adapters/"))

    def test_trailing_slash_glob_pattern_matches_files_below(self) -> None:
        from check_conformance_policy import path_matches

        self.assertTrue(path_matches("src/pkg/file.py", "src/*/"))
        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/py*/"))
        self.assertFalse(path_matches("conformance/adapters/go/foo.go", "conformance/adapters/py*/"))
        self.assertFalse(path_matches("lib/pkg/file.py", "src/*/"))

    def test_double_star_trailing_slash_pattern_matches_whole_tree(self) -> None:
        from check_conformance_policy import path_matches

        self.assertTrue(path_matches("conformance/adapters/README.md", "conformance/adapters/**/"))
        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/**/"))
        self.assertTrue(path_matches("/conformance/adapters/README.md", "/conformance/adapters/**/"))
        self.assertFalse(path_matches("conformance/adapters-old/foo.py", "conformance/adapters/**/"))

    def test_other_pattern_forms_unchanged(self) -> None:
        from check_conformance_policy import path_matches

        self.assertTrue(path_matches("conformance/adapters/python/foo.py", "conformance/adapters/**"))
        self.assertTrue(path_matches("conformance/adapters", "conformance/adapters/**"))
        self.assertTrue(path_matches("pkg/mpp/parse.go", "pkg/*/parse.go"))
        self.assertFalse(path_matches("pkg/mpp/parse.go", "pkg/server/*"))


if __name__ == "__main__":
    unittest.main()
