from __future__ import annotations

import unittest
from dataclasses import replace

from agricola.models import LabelResolution, Manifest, PullRequestFile
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
        self.assertIn("Agricola never writes", body)
        self.assertIn("**ruby** (`stripe/mpp-rb`): notify only", body)
        self.assertNotIn("@agricola propagate", body)

    def test_plan_surfaces_label_errors(self) -> None:
        labels = LabelResolution(
            ("agricola:golang",), (), errors=("unknown label: agricola:golang",)
        )
        body = build_tracking_issue(change(), labels, manifest())
        self.assertIn("Error: **unknown label: agricola:golang**", body)

    def test_plan_determines_sdk_applicability_from_capabilities(self) -> None:
        payload = manifest().model_dump(mode="json")
        payload["sdks"]["go"]["capabilities"] = ["intents", "refunds"]
        payload["sdks"]["rust"]["capabilities"] = ["intents"]
        configured = Manifest.model_validate(payload)
        source = replace(
            change(),
            files=(PullRequestFile("src/refunds.ts", additions=20),),
        )

        body = build_tracking_issue(source, LabelResolution((), ()), configured)

        self.assertIn(
            "**go** (`tempoxyz/mpp-go`): applicable: supports `refunds`",
            body,
        )
        self.assertIn(
            "**rust** (`tempoxyz/mpp-rs`): not applicable: missing declared `refunds`",
            body,
        )

    def test_title_is_stable(self) -> None:
        self.assertEqual(
            tracking_issue_title(change()), "[Agricola] mppx#412: Add refunds"
        )


if __name__ == "__main__":
    unittest.main()
