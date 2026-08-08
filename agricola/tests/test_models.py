from __future__ import annotations

import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from agricola.models import Cursor, PropagateDecision, SkipDecision
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


if __name__ == "__main__":
    unittest.main()
