from __future__ import annotations

import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from agricola.models import (
    Changelog,
    Cursor,
    PropagateDecision,
    PropagationRequest,
    PropagationRevision,
    SkipDecision,
    Source,
)
from agricola.tests.helpers import change


class CursorTests(unittest.TestCase):
    def test_advance_keeps_latest_merge_time(self) -> None:
        cursor = Cursor(merged_at=datetime(2026, 8, 7, 13, 0, tzinfo=UTC))
        advanced = cursor.advance(change())
        self.assertEqual(advanced.merged_at, change().merged_at)
        self.assertEqual(advanced.advance(change(413)), advanced)

    def test_json_round_trip(self) -> None:
        original = Cursor(merged_at=datetime(2026, 8, 7, tzinfo=UTC))
        self.assertEqual(
            Cursor.model_validate_json(original.model_dump_json()), original
        )

    def test_requires_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            Cursor(merged_at=datetime(2026, 8, 7, 14, 0))  # noqa: DTZ001


class DecisionTests(unittest.TestCase):
    def test_skip_requires_reason_and_forbids_pr(self) -> None:
        with self.assertRaises(ValidationError):
            SkipDecision.model_validate(
                {
                    "target": "go",
                    "decision": "skip",
                    "by": "maintainer",
                    "idempotency_key": "comment:1",
                }
            )
        with self.assertRaises(ValidationError):
            SkipDecision.model_validate(
                {
                    "target": "go",
                    "decision": "skip",
                    "by": "maintainer",
                    "idempotency_key": "comment:1",
                    "reason": "not applicable",
                    "pr": "tempoxyz/mpp-go#1",
                }
            )

    def test_propagate_requires_pr_and_forbids_reason(self) -> None:
        with self.assertRaises(ValidationError):
            PropagateDecision.model_validate(
                {
                    "target": "go",
                    "decision": "propagate",
                    "by": "maintainer",
                    "idempotency_key": "comment:1",
                }
            )
        with self.assertRaises(ValidationError):
            PropagateDecision.model_validate(
                {
                    "target": "go",
                    "decision": "propagate",
                    "by": "maintainer",
                    "idempotency_key": "comment:1",
                    "pr": "tempoxyz/mpp-go#1",
                    "reason": "unexpected",
                }
            )


class PropagationRequestTests(unittest.TestCase):
    def values(self) -> dict[str, object]:
        return {
            "source": Source(repo="wevm/mppx", pr=1, sha="abc1234567"),
            "source_title": "Change",
            "source_url": "https://example.test/source",
            "target": "go",
            "target_repo": "tempoxyz/mpp-go",
            "target_base_sha": "def1234567",
            "tracking_issue": 1,
            "tracking_issue_url": "https://example.test/issue",
            "by": "maintainer",
            "idempotency_key": "revision:1",
            "branch": "agricola/mppx-1",
            "verify": ("make test",),
            "changelog": Changelog.KEEP_A_CHANGELOG,
            "plan": "plan",
            "revision": PropagationRevision(
                pr="tempoxyz/mpp-go#1",
                url="https://example.test/pr",
                head_sha="def1234567",
            ),
        }

    def test_revision_requires_instruction(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires an instruction"):
            PropagationRequest.model_validate(self.values())

    def test_revision_requires_exact_head(self) -> None:
        values = self.values()
        values["instruction"] = "address CI"
        values["target_base_sha"] = "other1234567"

        with self.assertRaisesRegex(ValidationError, "head must equal"):
            PropagationRequest.model_validate(values)


if __name__ == "__main__":
    unittest.main()
