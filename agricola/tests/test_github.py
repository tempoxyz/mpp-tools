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
    def test_repo_token_overrides_control_plane_token(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "[]", "")
        client = GitHubClient(
            "tempoxyz/mpp-tools",
            token="control-token",
            repo_tokens={"wevm/mppx": "canonical-token"},
        )

        client.api("repos/wevm/mppx/issues/1/events")

        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "canonical-token")

    @patch("agricola.github.subprocess.run")
    def test_failed_run_log_uses_repository_token(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "failed output", "")
        client = GitHubClient(
            "tempoxyz/mpp-tools",
            token="control-token",
            repo_tokens={"tempoxyz/pympp": "sdk-token"},
        )

        result = client.failed_run_log("tempoxyz/pympp", 123)

        self.assertEqual(result, "failed output")
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "sdk-token")
        self.assertEqual(
            run.call_args.args[0],
            [
                "gh",
                "run",
                "view",
                "123",
                "--repo",
                "tempoxyz/pympp",
                "--log-failed",
            ],
        )

    @patch("agricola.github.subprocess.run")
    def test_post_fields_are_json_body(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, '{"number": 1}', "")
        client = GitHubClient("tempoxyz/mpp-tools")
        client.api("repos/test/test/issues", method="POST", fields={"title": "Title"})
        command = run.call_args.args[0]
        self.assertIn("--input", command)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"title": "Title"})

    def test_create_issue_provisions_and_applies_missing_labels(self) -> None:
        client = StubClient(
            [
                [[{"name": "go"}]],
                {"name": "agricola"},
                {"number": 1},
            ]
        )

        issue = client.create_issue("Title", "Body", ("agricola", "go"))

        self.assertEqual(issue["number"], 1)
        self.assertEqual(
            client.calls[0][0], "repos/tempoxyz/mpp-tools/labels?per_page=100"
        )
        self.assertTrue(client.calls[0][1]["paginate"])
        self.assertEqual(
            client.calls[1],
            (
                "repos/tempoxyz/mpp-tools/labels",
                {
                    "method": "POST",
                    "fields": {
                        "name": "agricola",
                        "color": "6f42c1",
                        "description": "Issues managed by Agricola",
                    },
                },
            ),
        )
        self.assertEqual(client.calls[2][1]["fields"]["labels"], ["agricola", "go"])

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

    def test_pull_request_rejects_unmerged_source(self) -> None:
        client = StubClient(
            [
                pull(
                    3,
                    merged_at=None,
                    updated_at="2026-08-07T14:20:00Z",
                )
            ]
        )

        with self.assertRaisesRegex(GitHubError, "is not merged"):
            client.pull_request("wevm/mppx", 3)

        self.assertEqual(len(client.calls), 1)

    def test_repository_head_resolves_default_branch_commit(self) -> None:
        client = StubClient([{"default_branch": "main"}, {"sha": "abc1234567"}])

        self.assertEqual(client.repository_head("tempoxyz/mpp-go"), "abc1234567")
        self.assertEqual(
            [endpoint for endpoint, _ in client.calls],
            ["repos/tempoxyz/mpp-go", "repos/tempoxyz/mpp-go/commits/main"],
        )

    def test_pull_revision_requires_open_stable_branch(self) -> None:
        client = StubClient(
            [
                {
                    "state": "open",
                    "html_url": "https://github.com/tempoxyz/mpp-go/pull/88",
                    "head": {
                        "sha": "def1234567",
                        "ref": "agricola/mppx-412",
                        "repo": {"full_name": "tempoxyz/mpp-go"},
                    },
                }
            ]
        )

        revision = client.pull_revision("tempoxyz/mpp-go#88", "agricola/mppx-412")

        self.assertEqual(revision.head_sha, "def1234567")

    def test_pull_revision_rejects_wrong_branch(self) -> None:
        client = StubClient(
            [
                {
                    "state": "open",
                    "html_url": "https://github.com/tempoxyz/mpp-go/pull/88",
                    "head": {
                        "sha": "def1234567",
                        "ref": "someone/else",
                        "repo": {"full_name": "tempoxyz/mpp-go"},
                    },
                }
            ]
        )

        with self.assertRaisesRegex(GitHubError, "does not use"):
            client.pull_revision("tempoxyz/mpp-go#88", "agricola/mppx-412")

    def test_pull_request_comments_expose_eyes_acknowledgement(self) -> None:
        client = StubClient(
            [
                [
                    [
                        {
                            "id": 55,
                            "body": "/ag fix add coverage",
                            "created_at": "2026-08-09T03:22:26+00:00",
                            "user": {"login": "maintainer"},
                            "reactions": {"eyes": 1},
                        },
                        {
                            "id": 56,
                            "body": "/ag fix add another test",
                            "created_at": "2026-08-09T03:23:26+00:00",
                            "user": {"login": "maintainer"},
                            "reactions": {"eyes": 0},
                        },
                    ]
                ]
            ]
        )

        comments = client.pull_request_comments("tempoxyz/mpp-go#88")

        self.assertTrue(comments[0].has_eyes)
        self.assertFalse(comments[1].has_eyes)
        endpoint, options = client.calls[0]
        self.assertEqual(
            endpoint, "repos/tempoxyz/mpp-go/issues/88/comments?per_page=100"
        )
        self.assertTrue(options["paginate"])

    def test_reacts_to_issue_comment_in_selected_repository(self) -> None:
        client = StubClient([{"id": 1, "content": "eyes"}])

        client.react_to_issue_comment("tempoxyz/mpp-go", 55)

        self.assertEqual(
            client.calls,
            [
                (
                    "repos/tempoxyz/mpp-go/issues/comments/55/reactions",
                    {"method": "POST", "fields": {"content": "eyes"}},
                )
            ],
        )

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

    def test_tracking_issue_listing_returns_every_marker_match(self) -> None:
        marker = "<!-- agricola:audit"
        client = StubClient(
            [
                [
                    [
                        {"number": 9, "body": "<!-- agricola:audit -->"},
                        {
                            "number": 10,
                            "body": "<!-- agricola:audit-finding=AGR-2026-001 -->",
                        },
                        {"number": 11, "body": "unrelated"},
                    ]
                ]
            ]
        )

        issues = client.find_tracking_issues(marker)

        self.assertEqual([issue["number"] for issue in issues], [9, 10])


if __name__ == "__main__":
    unittest.main()
