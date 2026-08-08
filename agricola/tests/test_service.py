from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime

from agricola.github import GitHubError
from agricola.ledger import CursorStore, DecisionLedger
from agricola.models import Cursor, LabelAction, LabelEvent
from agricola.service import handle_comment, poll
from agricola.tests.helpers import change, manifest


class FakeGitHub:
    def __init__(self) -> None:
        self.change = change()
        self.tracking = None
        self.created: list[tuple[str, str]] = []
        self.updated: list[tuple[int, str | None, str | None]] = []
        self.comments: list[tuple[int, str]] = []
        self.pull_requests = 0
        self.events: tuple[LabelEvent, ...] = (
            LabelEvent(
                LabelAction.LABELED,
                "agricola:go",
                "maintainer",
                datetime(2026, 8, 7, 13, 59, tzinfo=UTC),
            ),
        )

    def merged_changes(self, repo, cursor):
        return [self.change]

    def pull_request(self, repo, number):
        self.pull_requests += 1
        return self.change

    def label_events(self, repo, number):
        return self.events

    def find_tracking_issue(self, marker):
        return self.tracking

    def create_issue(self, title, body, labels=()):
        self.created.append((title, body))
        return {"number": 99, "title": title, "body": body}

    def update_issue(self, number, *, title=None, body=None):
        self.updated.append((number, title, body))
        return {"number": number}

    def comment_issue(self, number, body):
        self.comments.append((number, body))
        return {"id": len(self.comments)}

    def source_from_body(self, body):
        if "agricola:source=" not in body:
            raise GitHubError("missing source")
        return "wevm/mppx", 412

    def pull_status(self, reference):
        return f"{reference}: open"


def cursor_store(directory: str) -> CursorStore:
    store = CursorStore(directory)
    store.save(Cursor(merged_at=datetime(2026, 8, 7, 13, 0, tzinfo=UTC)))
    return store


class PollTests(unittest.TestCase):
    def test_creates_once_and_commits_cursor_with_ledger_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            ledger = DecisionLedger(directory)
            store = cursor_store(directory)

            first = poll(client, manifest(), ledger, store)
            second = poll(client, manifest(), ledger, store)

            self.assertEqual(first.created, 1)
            self.assertEqual(second.deduplicated, 1)
            self.assertEqual(len(client.created), 1)
            self.assertEqual(client.pull_requests, 1)
            self.assertEqual(store.load().merged_at, client.change.merged_at)
            self.assertIsNotNone(ledger.read("wevm/mppx", 412))

    def test_deduplicates_by_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            client.tracking = {"number": 4}
            result = poll(
                client,
                manifest(),
                DecisionLedger(directory),
                cursor_store(directory),
            )
            self.assertEqual(result.deduplicated, 1)
            self.assertFalse(client.created)

    def test_clean_none_suppresses_tracking_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            client.change = replace(client.change, labels=("agricola:none",))
            client.events = (
                LabelEvent(
                    LabelAction.LABELED,
                    "agricola:none",
                    "maintainer",
                    datetime(2026, 8, 7, 13, 59, tzinfo=UTC),
                ),
            )
            result = poll(
                client,
                manifest(),
                DecisionLedger(directory),
                cursor_store(directory),
            )
            self.assertEqual(result.suppressed, 1)
            self.assertFalse(client.created)

    def test_none_conflict_creates_diagnostic_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            client.change = replace(
                client.change, labels=("agricola:none", "agricola:go")
            )
            client.events = tuple(
                LabelEvent(
                    LabelAction.LABELED,
                    label,
                    "maintainer",
                    datetime(2026, 8, 7, 13, 59, tzinfo=UTC),
                )
                for label in client.change.labels
            )
            result = poll(
                client,
                manifest(),
                DecisionLedger(directory),
                cursor_store(directory),
            )
            self.assertEqual(result.created, 1)
            self.assertIn("agricola:none overrides agricola:go", client.created[0][1])

    def test_ledger_records_merge_time_labels_not_current_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            client.change = replace(client.change, labels=())
            client.events = (
                *client.events,
                LabelEvent(
                    LabelAction.UNLABELED,
                    "agricola:go",
                    "maintainer",
                    datetime(2026, 8, 7, 14, 1, tzinfo=UTC),
                ),
            )
            ledger = DecisionLedger(directory)

            poll(client, manifest(), ledger, cursor_store(directory))

            entry = ledger.read("wevm/mppx", 412)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.labels, ("agricola:go",))


class CommentTests(unittest.TestCase):
    def event(self, body: str, author: str = "maintainer", comment_id: int = 55):
        return {
            "comment": {
                "id": comment_id,
                "body": body,
                "created_at": "2026-08-07T14:02:00Z",
                "user": {"login": author},
            },
            "issue": {"number": 207, "body": "<!-- agricola:source=wevm/mppx#412 -->"},
        }

    def test_plan_regenerates_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            result = handle_comment(
                client,
                manifest(),
                DecisionLedger(directory),
                self.event("@agricola plan"),
            )
            self.assertEqual(result.commands, 1)
            self.assertEqual(client.updated[0][0], 207)
            self.assertIsNotNone(result.reply)
            assert result.reply is not None
            self.assertIn("Regenerated", result.reply.body)
            self.assertFalse(client.comments)

    def test_skip_records_once_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            client.change = replace(client.change, labels=())
            ledger = DecisionLedger(directory)
            event = self.event('@agricola skip ruby reason="TS-only tooling"')
            first = handle_comment(client, manifest(), ledger, event)
            second = handle_comment(client, manifest(), ledger, event)
            self.assertTrue(first.changed_ledger)
            self.assertFalse(second.changed_ledger)
            entry = ledger.read("wevm/mppx", 412)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(len(entry.decisions), 1)
            self.assertEqual(entry.labels, ("agricola:go",))
            self.assertIsNotNone(second.reply)
            assert second.reply is not None
            self.assertIn("Already recorded", second.reply.body)
            self.assertFalse(client.comments)

    def test_unauthorized_command_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            result = handle_comment(
                client,
                manifest(),
                DecisionLedger(directory),
                self.event("@agricola status", "outsider"),
            )
            self.assertTrue(result.ignored)
            self.assertFalse(client.comments)

    def test_unauthorized_malformed_command_cannot_trigger_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            result = handle_comment(
                client,
                manifest(),
                DecisionLedger(directory),
                self.event("@agricola destroy everything", "outsider"),
            )
            self.assertTrue(result.ignored)
            self.assertFalse(client.comments)

    def test_incidental_mention_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            result = handle_comment(
                client,
                manifest(),
                DecisionLedger(directory),
                self.event("please ask @agricola status"),
            )
            self.assertTrue(result.ignored)

    def test_command_outside_tracking_issue_gets_scope_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub()
            event = self.event("@agricola status")
            event["issue"]["body"] = "ordinary issue"
            result = handle_comment(
                client, manifest(), DecisionLedger(directory), event
            )
            self.assertEqual(result.commands, 1)
            self.assertIsNotNone(result.reply)
            assert result.reply is not None
            self.assertIn("require a tracking issue", result.reply.body)
            self.assertFalse(client.comments)


if __name__ == "__main__":
    unittest.main()
