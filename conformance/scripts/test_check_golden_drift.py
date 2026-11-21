from __future__ import annotations

import unittest

from check_golden_drift import drift_errors

GOLDEN = "conformance/flows/golden-results.json"
SNAPSHOTS = "conformance/pairs/snapshots.json"


class GoldenDriftTests(unittest.TestCase):
    def test_no_golden_change_passes(self) -> None:
        self.assertEqual(drift_errors(["conformance/vectors/receipt.json"], "0.11.0", "0.11.0"), [])

    def test_golden_change_alone_fails(self) -> None:
        errors = drift_errors([GOLDEN, SNAPSHOTS, "README.md"], "0.11.0", "0.11.0")
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith(GOLDEN))
        self.assertTrue(errors[1].startswith(SNAPSHOTS))

    def test_mppx_pin_change_explains_goldens(self) -> None:
        self.assertEqual(drift_errors([GOLDEN, "conformance/package.json"], "0.11.0", "0.21.1"), [])

    def test_lockfile_only_change_does_not_explain_goldens(self) -> None:
        self.assertNotEqual(drift_errors([GOLDEN, "conformance/package-lock.json", "conformance/package.json"], "0.11.0", "0.11.0"), [])

    def test_fixture_changes_explain_goldens(self) -> None:
        for fixture in (
            "conformance/flows/flows.json",
            "conformance/pairs/scenarios.json",
            "conformance/golden/adapter.ts",
            "conformance/scripts/pair_runner.py",
        ):
            with self.subTest(fixture=fixture):
                self.assertEqual(drift_errors([SNAPSHOTS, fixture], "0.11.0", "0.11.0"), [])

    def test_one_golden_does_not_explain_the_other(self) -> None:
        self.assertNotEqual(drift_errors([GOLDEN, SNAPSHOTS], "0.11.0", "0.11.0"), [])


if __name__ == "__main__":
    unittest.main()
