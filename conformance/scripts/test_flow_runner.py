from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from flow_runner import perform_request


class PerformRequestTests(unittest.TestCase):
    def test_put_and_patch_send_configured_body(self) -> None:
        for method in ("PUT", "PATCH"):
            with self.subTest(method=method):
                response = MagicMock()
                response.__enter__.return_value.status = 200
                response.__enter__.return_value.headers = {}
                response.__enter__.return_value.read.return_value = b""

                with patch("flow_runner.urllib.request.urlopen", return_value=response) as urlopen:
                    perform_request(
                        "https://example.com/paid",
                        {"http_method": method, "body": '{"amount":"1"}'},
                    )

                request = urlopen.call_args.args[0]
                self.assertEqual(request.data, b'{"amount":"1"}')
                self.assertEqual(request.get_header("Content-type"), "application/json")


if __name__ == "__main__":
    unittest.main()
