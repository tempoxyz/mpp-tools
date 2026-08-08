from __future__ import annotations

import unittest

from agricola.models import (
    DecisionKind,
    LabelResolution,
    PropagateDecision,
    PullRequestFile,
    SkipDecision,
)
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
        self.assertIn("```text\n/ag fix\n```", body)
        self.assertIn("What it does", body)
        self.assertIn("/ag skip go reason=", body)
        self.assertIn("| `go` | pr | Queued | — |", body)
        self.assertIn("| `rust` | pr | Awaiting decision | — |", body)
        self.assertIn("| `ruby` | notify | Notification only | — |", body)

    def test_plan_table_renders_recorded_decisions(self) -> None:
        decisions = (
            PropagateDecision(
                target="go",
                decision=DecisionKind.PROPAGATE,
                by="maintainer",
                pr="tempoxyz/mpp-go#88",
                idempotency_key="propagate:mppx#412:go",
            ),
            SkipDecision(
                target="rust",
                decision=DecisionKind.SKIP,
                by="maintainer",
                reason="not | applicable",
                idempotency_key="skip:mppx#412:rust",
            ),
        )

        body = build_tracking_issue(
            change(), LabelResolution((), ()), manifest(), decisions=decisions
        )

        self.assertIn(
            "| `go` | pr | Recorded | "
            "[tempoxyz/mpp-go#88](https://github.com/tempoxyz/mpp-go/pull/88) |",
            body,
        )
        self.assertIn("| `rust` | pr | Skipped — not \\| applicable | — |", body)

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
