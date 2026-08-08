from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agricola.cli import main
from agricola.tests.test_service import FakeGitHub


class CliTests(unittest.TestCase):
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
