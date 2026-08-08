from __future__ import annotations

import unittest

from agricola.models import LabelResolution, PullRequestFile
from agricola.planner import (
    FileCategory,
    build_tracking_issue,
    classify_file,
    tracking_issue_title,
)
from agricola.tests.helpers import change, manifest


class PlannerTests(unittest.TestCase):
    def test_classifies_paths(self) -> None:
        cases = {
            "conformance/vectors/refund.json": FileCategory.NORMATIVE,
            "src/refund.ts": FileCategory.CANONICAL,
            "src/speculative.ts": FileCategory.CANONICAL,
            "src/vectorized.ts": FileCategory.CANONICAL,
            "package-lock.json": FileCategory.INCIDENTAL,
            ".github/workflows/test.yml": FileCategory.INCIDENTAL,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_file(PullRequestFile(path)), expected)

    def test_plan_contains_marker_sections_and_safety_boundary(self) -> None:
        labels = LabelResolution(("agricola:go",), ("go",))
        body = build_tracking_issue(change(), labels, manifest())
        self.assertIn("<!-- agricola:source=wevm/mppx#412 -->", body)
        self.assertIn("Normative specification requirements", body)
        self.assertIn("Canonical behavior worth matching", body)
        self.assertIn("Incidental TypeScript", body)
        self.assertIn("creates draft downstream PRs only", body)
        self.assertIn("Draft PR automation: `go`, `rust`", body)
        self.assertIn("Notification only: `ruby`", body)
        self.assertIn("@agricola propagate all", body)

    def test_plan_surfaces_label_errors(self) -> None:
        labels = LabelResolution(
            ("agricola:golang",), (), errors=("unknown label: agricola:golang",)
        )
        body = build_tracking_issue(change(), labels, manifest())
        self.assertIn("Error: **unknown label: agricola:golang**", body)

    def test_title_is_stable(self) -> None:
        self.assertEqual(
            tracking_issue_title(change()), "[Agricola] mppx#412: Add refunds"
        )


if __name__ == "__main__":
    unittest.main()
