#!/usr/bin/env python3
"""Tests for the daily SDK conformance issue reporter."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import report_sdk_conformance as report


def needs(**results: tuple[str, str]) -> str:
    return json.dumps({job: {"result": result, "outputs": {"sdk_sha": sha}} for job, (result, sha) in results.items()})


class FakeGh:
    def __init__(self, open_issues: list[dict], seen: str = "") -> None:
        self.open_issues = open_issues
        self.seen = seen
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, token: str | None = None) -> str:
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(self.open_issues)
        if args[:2] == ("issue", "view"):
            return self.seen
        if args[0] == "api":
            return json.dumps(
                {
                    "name": "mpp-rs / rust conformance",
                    "html_url": "https://github.com/tempoxyz/mpp-tools/actions/runs/1/job/2",
                    "steps": [
                        {"name": "Run vector conformance", "conclusion": "failure"},
                        {"name": "Checkout SDK", "conclusion": "success"},
                    ],
                }
            )
        return ""

    def verbs(self) -> list[tuple[str, str]]:
        return [call[:2] for call in self.calls if call[0] != "api"]


ENV = {"GH_REPO": "tempoxyz/mpp-tools", "RUN_ID": "1", "TOOLS_SHA": "f" * 40}
RUST_TITLE = "Conformance failing: tempoxyz/mpp-rs (rust)"


class ReportTest(unittest.TestCase):
    def run_report(self, fake: FakeGh, **results: tuple[str, str]) -> None:
        with patch.dict(os.environ, {**ENV, "NEEDS": needs(**results)}), patch.object(report, "gh", fake):
            report.main()

    def test_opens_one_issue_for_a_failing_sdk(self) -> None:
        fake = FakeGh([])
        self.run_report(fake, **{"sdk-rust": ("failure", "a" * 40), "sdk-go": ("success", "b" * 40)})

        creates = [call for call in fake.calls if call[:2] == ("issue", "create")]
        self.assertEqual(len(creates), 1)
        create = creates[0]
        self.assertIn(RUST_TITLE, create)
        body = create[create.index("--body") + 1]
        self.assertIn("aaaaaaaaaaaa", body)
        self.assertIn("Run vector conformance", body)
        self.assertIn("make use-local-sdk ADAPTER=rust", body)
        self.assertIn(("label", "create"), fake.verbs())
        self.assertIn("(cd adapters/rust && cargo fetch)", body)

    def test_typescript_reproduction_builds_the_sdk_checkout(self) -> None:
        fake = FakeGh([])
        self.run_report(fake, **{"sdk-typescript": ("failure", "d" * 40)})
        create = next(call for call in fake.calls if call[:2] == ("issue", "create"))
        body = create[create.index("--body") + 1]
        self.assertLess(body.index("pnpm build"), body.index("make use-local-sdk"))
        self.assertLess(body.index("make use-local-sdk"), body.index("\nnpm install\n"))

    def test_comments_once_per_new_failing_commit(self) -> None:
        fake = FakeGh([{"number": 7, "title": RUST_TITLE}], seen="earlier " + "a" * 40)
        self.run_report(fake, **{"sdk-rust": ("failure", "a" * 40)})
        self.assertNotIn(("issue", "comment"), fake.verbs())

        fake = FakeGh([{"number": 7, "title": RUST_TITLE}], seen="earlier " + "a" * 40)
        self.run_report(fake, **{"sdk-rust": ("failure", "c" * 40)})
        self.assertIn(("issue", "comment"), fake.verbs())
        self.assertNotIn(("issue", "create"), fake.verbs())

    def test_comments_when_the_failing_commit_is_unknown(self) -> None:
        fake = FakeGh([{"number": 7, "title": RUST_TITLE}], seen="earlier body")
        self.run_report(fake, **{"sdk-rust": ("failure", "")})
        self.assertIn(("issue", "comment"), fake.verbs())

    def test_closes_the_issue_once_the_sdk_passes(self) -> None:
        fake = FakeGh([{"number": 7, "title": RUST_TITLE}])
        self.run_report(fake, **{"sdk-rust": ("success", "a" * 40)})
        self.assertIn(("issue", "close", "7"), [call[:3] for call in fake.calls])

    def test_ignores_cancelled_and_skipped_runs(self) -> None:
        fake = FakeGh([{"number": 7, "title": RUST_TITLE}])
        self.run_report(fake, **{"sdk-rust": ("cancelled", ""), "sdk-go": ("skipped", "")})
        self.assertEqual(fake.verbs(), [("issue", "list")])


if __name__ == "__main__":
    unittest.main()
