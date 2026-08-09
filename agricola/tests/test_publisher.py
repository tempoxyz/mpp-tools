from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any

from agricola.models import PropagationRequest, PropagationRevision
from agricola.publisher import PublicationError, publish


def request(*, revision: bool = False) -> PropagationRequest:
    base = PropagationRequest(
        source={"repo": "wevm/mppx", "pr": 412, "sha": "abc1234567"},
        source_title="Add refunds",
        source_url="https://github.com/wevm/mppx/pull/412",
        target="go",
        target_repo="tempoxyz/mpp-go",
        target_base_sha="def4567890",
        tracking_issue=207,
        tracking_issue_url="https://github.com/tempoxyz/mpp-tools/issues/207",
        by="maintainer",
        idempotency_key="propagate:mppx#412:go",
        branch="agricola/mppx-412",
        verify=("make test",),
        changelog="keep-a-changelog",
        plan="Plan",
    )
    if not revision:
        return base
    head = "fedcba9876"
    return base.model_copy(
        update={
            "target_base_sha": head,
            "instruction": "address review feedback",
            "idempotency_key": "revise:mppx#412:go:99:1",
            "revision": PropagationRevision(
                pr="tempoxyz/mpp-go#88",
                url="https://github.com/tempoxyz/mpp-go/pull/88",
                head_sha=head,
            ),
        }
    )


def pull(
    *, number: int = 88, state: str = "open", draft: bool = True, merged: bool = False
) -> dict[str, object]:
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "merged_at": "2026-08-08T00:00:00Z" if merged else None,
        "html_url": f"https://github.com/tempoxyz/mpp-go/pull/{number}",
        "node_id": f"PR_{number}",
        "head": {"repo": {"full_name": "tempoxyz/mpp-go"}},
    }


class FakeGit:
    def __init__(self, remote: str | None = None) -> None:
        self.remote = remote
        self.ancestor = True
        self.commits: tuple[str, ...] = ()
        self.trailers: dict[tuple[str, str], str] = {}
        self.stats: dict[str, str] = {}
        self.calls: list[tuple[object, ...]] = []

    def fetch_branch(self, branch: str) -> str:
        self.calls.append(("fetch", branch))
        assert self.remote is not None
        return self.remote

    def remote_branch_sha(self, branch: str) -> str | None:
        self.calls.append(("remote", branch))
        return self.remote

    def push(self, branch: str, *, expected_remote: str | None = None) -> None:
        self.calls.append(("push", branch, expected_remote))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        self.calls.append(("ancestor", ancestor, descendant))
        return self.ancestor

    def commits_between(self, ancestor: str, descendant: str) -> tuple[str, ...]:
        self.calls.append(("commits", ancestor, descendant))
        return self.commits

    def trailer(self, commit: str, key: str) -> str:
        return self.trailers.get((commit, key), "")

    def stat(self, commit: str) -> str:
        return self.stats[commit]


class FakeGitHub:
    def __init__(self, pulls: list[dict[str, object]] | None = None) -> None:
        self.pulls = pulls or []
        self.comments: list[dict[str, object]] = []
        self.created: list[dict[str, object]] = []
        self.graphql_calls: list[dict[str, object]] = []

    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, object] | None = None,
        paginate: bool = False,
    ) -> Any:
        if endpoint == "repos/tempoxyz/mpp-go/pulls" and method == "GET":
            return self.pulls
        if endpoint == "repos/tempoxyz/mpp-go" and method == "GET":
            return {"default_branch": "main"}
        if endpoint == "repos/tempoxyz/mpp-go/pulls" and method == "POST":
            assert fields is not None
            self.created.append(fields)
            return pull(number=99)
        if (
            endpoint.split("?", 1)[0].endswith("/comments")
            and method == "GET"
            and paginate
        ):
            return [self.comments]
        if endpoint.endswith("/comments") and method == "POST":
            assert fields is not None
            self.comments.append({"body": fields["body"]})
            return self.comments[-1]
        raise AssertionError((endpoint, method, fields, paginate))

    def graphql(self, query: str, variables: dict[str, object]) -> object:
        self.graphql_calls.append(variables)
        return {"data": {"convertPullRequestToDraft": {"pullRequest": {}}}}


class PublisherTests(unittest.TestCase):
    def test_creates_new_stable_branch_and_draft_pull(self) -> None:
        git = FakeGit()
        github = FakeGitHub()

        result = publish(
            request(),
            title="Add refunds",
            body="Body",
            git=git,
            github=github,
            at=datetime(2026, 8, 8, tzinfo=UTC),
        )

        self.assertEqual(result.pr, "tempoxyz/mpp-go#99")
        self.assertIn(("push", "agricola/mppx-412", None), git.calls)
        self.assertEqual(
            github.created,
            [
                {
                    "title": "Add refunds",
                    "body": "Body",
                    "head": "agricola/mppx-412",
                    "base": "main",
                    "draft": True,
                }
            ],
        )

    def test_updates_existing_branch_with_lease_and_returns_pull_to_draft(self) -> None:
        git = FakeGit("remote123")
        github = FakeGitHub([pull(draft=False)])

        result = publish(request(), title="Title", body="Body", git=git, github=github)

        self.assertEqual(result.pr, "tempoxyz/mpp-go#88")
        self.assertIn(
            ("push", "agricola/mppx-412", "remote123"),
            git.calls,
        )
        self.assertEqual(github.graphql_calls, [{"id": "PR_88"}])
        self.assertFalse(github.created)

    def test_merged_pull_is_already_complete(self) -> None:
        git = FakeGit()
        github = FakeGitHub([pull(merged=True)])

        result = publish(request(), title="Title", body="Body", git=git, github=github)

        self.assertEqual(result.pr, "tempoxyz/mpp-go#88")
        self.assertFalse(git.calls)

    def test_closed_pull_must_be_reopened(self) -> None:
        with self.assertRaisesRegex(PublicationError, "reopen"):
            publish(
                request(),
                title="Title",
                body="Body",
                git=FakeGit(),
                github=FakeGitHub([pull(state="closed")]),
            )

    def test_publishes_revision_and_posts_summary_once(self) -> None:
        item = request(revision=True)
        assert item.revision is not None
        git = FakeGit(item.revision.head_sha)
        github = FakeGitHub([pull()])

        publish(
            item,
            title="Title",
            body="Body",
            git=git,
            github=github,
            revision_summary="Fixed parser\nAdded regression test",
            revision_stat="src/parser.py | 2 ++\n",
        )

        self.assertIn(("push", item.branch, None), git.calls)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("- Fixed parser", str(github.comments[0]["body"]))
        self.assertIn("src/parser.py | 2 ++", str(github.comments[0]["body"]))

        publish(
            item,
            title="Title",
            body="Body",
            git=git,
            github=github,
            revision_summary="Fixed parser",
            revision_stat="stat",
        )
        self.assertEqual(len(github.comments), 1)

    def test_resumes_revision_already_published_with_same_request_key(self) -> None:
        item = request(revision=True)
        git = FakeGit("newremote123")
        git.commits = ("published123",)
        git.trailers[("published123", "Agricola-Request")] = item.idempotency_key
        git.trailers[("published123", "Agricola-Summary")] = "Stored summary"
        git.stats["published123"] = "stored stat\n"
        github = FakeGitHub([pull()])

        publish(
            item,
            title="Title",
            body="Body",
            git=git,
            github=github,
            revision_summary="New summary",
            revision_stat="new stat",
        )

        self.assertFalse(any(call[0] == "push" for call in git.calls))
        self.assertIn("Stored summary", str(github.comments[0]["body"]))
        self.assertIn("stored stat", str(github.comments[0]["body"]))

    def test_rejects_unrelated_revision_branch_change(self) -> None:
        item = request(revision=True)
        git = FakeGit("newremote123")
        git.ancestor = False

        with self.assertRaisesRegex(PublicationError, "no longer descends"):
            publish(
                item,
                title="Title",
                body="Body",
                git=git,
                github=FakeGitHub([pull()]),
                revision_summary="Summary",
                revision_stat="stat",
            )


if __name__ == "__main__":
    unittest.main()
