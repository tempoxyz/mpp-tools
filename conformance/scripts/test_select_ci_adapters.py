from __future__ import annotations

import unittest

from select_ci_adapters import ADAPTER_ORDER, select_adapters


class SelectAdaptersTests(unittest.TestCase):
    def test_selects_expected_scope(self) -> None:
        cases = [
            (
                "Agricola workflow and implementation",
                "pull_request",
                [
                    ".github/workflows/agricola.yml",
                    "agricola/service.py",
                    "agricola/tests/test_service.py",
                    "docs/agricola-actions.md",
                    "sdks.yaml",
                ],
                [],
                "only Agricola control-plane files changed",
            ),
            (
                "Agricola state pull request",
                "pull_request",
                ["ledger/cursor.json", "ledger/wevm-mppx-123.json"],
                [],
                "only Agricola control-plane files changed",
            ),
            (
                "mixed Agricola and conformance workflow",
                "pull_request",
                [".github/workflows/agricola.yml", ".github/workflows/conformance.yml"],
                ADAPTER_ORDER,
                "shared conformance files changed",
            ),
            (
                "adapter implementation",
                "pull_request",
                ["conformance/adapters/python/server.py"],
                ["python"],
                "adapter-specific conformance files changed",
            ),
            (
                "unrelated files",
                "pull_request",
                ["LICENSE"],
                [],
                "no conformance-affecting files changed",
            ),
            (
                "push event",
                "push",
                ["agricola/service.py"],
                ADAPTER_ORDER,
                "non-PR event runs the full suite",
            ),
        ]

        for name, event_name, changed_files, expected, reason in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    select_adapters(event_name, changed_files),
                    (expected, reason),
                )


if __name__ == "__main__":
    unittest.main()
