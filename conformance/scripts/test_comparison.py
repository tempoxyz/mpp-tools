from __future__ import annotations

import unittest

from comparison import format_json_mismatch, json_diff


class JsonComparisonTests(unittest.TestCase):
    def test_equal_values_have_no_diff(self) -> None:
        self.assertEqual(json_diff({"value": [1, 2]}, {"value": [1, 2]}), "")

    def test_array_order_can_be_ignored_recursively(self) -> None:
        expected = {"items": [{"id": 1}, {"id": 2}]}
        actual = {"items": [{"id": 2}, {"id": 1}]}

        self.assertEqual(json_diff(expected, actual, ignore_order=True), "")
        self.assertNotEqual(json_diff(expected, actual), "")

    def test_mismatch_is_a_unified_json_diff(self) -> None:
        mismatch = format_json_mismatch({"status": "success"}, {"status": "failed"})

        self.assertIn("result mismatch:", mismatch)
        self.assertIn("--- expected", mismatch)
        self.assertIn("+++ actual", mismatch)
        self.assertIn('-  "status": "success"', mismatch)
        self.assertIn('+  "status": "failed"', mismatch)


if __name__ == "__main__":
    unittest.main()
