from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agricola.cli import main
from agricola.ledger import DecisionLedger
from agricola.models import PropagationRequest, PropagationResult
from agricola.tests.helpers import change
from agricola.tests.test_service import FakeGitHub


class CliTests(unittest.TestCase):
    def test_records_published_propagation_and_renders_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            replies = root / "replies"
            results.mkdir()
            ledger = DecisionLedger(root / "ledger")
            ledger.ensure(change())
            request = PropagationRequest(
                source={"repo": "wevm/mppx", "pr": 412, "sha": "abc1234567"},
                source_title="Change relay key",
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
                plan="Reviewed impact plan",
            )
            result = PropagationResult(
                request=request,
                pr="tempoxyz/mpp-go#88",
                url="https://github.com/tempoxyz/mpp-go/pull/88",
                at=datetime(2026, 8, 7, 14, 5, tzinfo=UTC),
            )
            (results / "go.json").write_text(result.model_dump_json())

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--ledger",
                        str(root / "ledger"),
                        "record-propagations",
                        str(results),
                        "--reply-directory",
                        str(replies),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Opened draft PR", (replies / "issue-207.json").read_text())
            entry = ledger.read("wevm/mppx", 412)
            assert entry is not None
            self.assertEqual(entry.decisions[0].pr, "tempoxyz/mpp-go#88")

    def test_deferred_reply_is_delivered_only_by_delivery_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root / "event.json"
            reply_path = root / "reply.json"
            event_path.write_text(
                json.dumps(
                    {
                        "comment": {
                            "id": 55,
                            "body": '@agricola skip ruby reason="TS-only tooling"',
                            "created_at": "2026-08-07T14:02:00Z",
                            "user": {"login": "brendanryan"},
                        },
                        "issue": {
                            "number": 207,
                            "body": "<!-- agricola:source=wevm/mppx#412 -->",
                        },
                    }
                )
            )
            client = FakeGitHub()
            with patch("agricola.cli.GitHubClient", return_value=client):
                with redirect_stdout(StringIO()):
                    result = main(
                        [
                            "--ledger",
                            str(root / "ledger"),
                            "handle-comment",
                            str(event_path),
                            "--reply-file",
                            str(reply_path),
                        ]
                    )
                self.assertEqual(result, 0)
                self.assertTrue(reply_path.exists())
                self.assertFalse(client.comments)

                with redirect_stdout(StringIO()):
                    delivered = main(["deliver-reply", str(reply_path)])

            self.assertEqual(delivered, 0)
            self.assertIn("Recorded skip", client.comments[0][1])


if __name__ == "__main__":
    unittest.main()
