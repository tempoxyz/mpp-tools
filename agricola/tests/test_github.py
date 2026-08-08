from __future__ import annotations

import json
import subprocess
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from agricola.github import GitHubClient, GitHubError
from agricola.models import Cursor


class StubClient(GitHubClient):
    def __init__(self, responses):
        super().__init__("tempoxyz/mpp-tools")
        self.responses = list(responses)
        self.calls = []

    def api(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self.responses.pop(0)


def pull(
    number: int,
    *,
    merged_at: str | None,
    updated_at: str,
    label: str | None = None,
):
    return {
        "number": number,
        "merged_at": merged_at,
        "updated_at": updated_at,
        "merge_commit_sha": f"abc123{number}",
        "title": f"PR {number}",
        "html_url": f"https://example.test/{number}",
        "body": "",
        "labels": [] if label is None else [{"name": label}],
    }


class GitHubApiTests(unittest.TestCase):
    @patch("agricola.github.subprocess.run")
    def test_get_fields_are_query_parameters(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, '{"items": []}', "")
        client = GitHubClient("tempoxyz/mpp-tools", token="token")
        client.api("search/issues", fields={"q": "repo:test/test", "per_page": 10})
        command = run.call_args.args[0]
        self.assertIn("q=repo:test/test", command)
        self.assertIn("per_page=10", command)
        self.assertIsNone(run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "token")

    @patch("agricola.github.subprocess.run")
    def test_post_fields_are_json_body(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, '{"number": 1}', "")
        client = GitHubClient("tempoxyz/mpp-tools")
        client.api("repos/test/test/issues", method="POST", fields={"title": "Title"})
        command = run.call_args.args[0]
        self.assertIn("--input", command)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"title": "Title"})

    @patch("agricola.github.subprocess.run")
    def test_api_failure_is_contextual(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "bad credentials")
        with self.assertRaisesRegex(GitHubError, "bad credentials"):
            GitHubClient("tempoxyz/mpp-tools").api("repos/test/test")

    def test_merged_changes_include_overlap_and_ignore_unmerged_pulls(self) -> None:
        cursor = Cursor(merged_at=datetime(2026, 8, 7, 14, 0, tzinfo=UTC))
        client = StubClient(
            [
                [
                    pull(
                        3,
                        merged_at=None,
                        updated_at="2026-08-07T14:20:00Z",
                    ),
                    pull(
                        2,
                        merged_at="2026-08-07T13:30:00Z",
                        updated_at="2026-08-07T14:10:00Z",
                        label="agricola:go",
                    ),
                    pull(
                        1,
                        merged_at="2026-08-07T12:59:59Z",
                        updated_at="2026-08-07T14:00:00Z",
                    ),
                ]
            ]
        )

        changes = client.merged_changes("wevm/mppx", cursor)

        self.assertEqual([item.number for item in changes], [2])
        self.assertEqual(changes[0].labels, ("agricola:go",))
        self.assertIn("state=closed", client.calls[0][0])
        self.assertIn("sort=updated", client.calls[0][0])

    def test_merged_changes_paginates_full_pages(self) -> None:
        cursor = Cursor(merged_at=datetime(2026, 8, 7, 14, 0, tzinfo=UTC))
        first_page = [
            pull(
                number,
                merged_at="2026-08-07T14:01:00Z",
                updated_at="2026-08-07T14:02:00Z",
            )
            for number in range(1, 101)
        ]
        second_page = [
            pull(
                101,
                merged_at="2026-08-07T14:03:00Z",
                updated_at="2026-08-07T14:03:00Z",
            )
        ]
        client = StubClient([first_page, second_page])

        changes = client.merged_changes("wevm/mppx", cursor)

        self.assertEqual(len(changes), 101)
        self.assertIn("page=2", client.calls[1][0])

    def test_tracking_issue_deduplication_uses_direct_issue_listing(self) -> None:
        marker = "<!-- agricola:source=wevm/mppx#412 -->"
        client = StubClient([[[{"number": 9, "body": marker}]]])

        issue = client.find_tracking_issue(marker)

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue["number"], 9)
        endpoint, options = client.calls[0]
        self.assertIn("repos/tempoxyz/mpp-tools/issues", endpoint)
        self.assertTrue(options["paginate"])


if __name__ == "__main__":
    unittest.main()
