#!/usr/bin/env python3
"""Tests for loading the server verification fixtures."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import server_verify_runner


class LoadCasesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(server_verify_runner.CASE_FILE.read_text(encoding="utf-8"))

    def load(self, document: object) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return server_verify_runner.load_cases(path)

    def test_checked_in_cases_are_valid(self) -> None:
        cases = server_verify_runner.load_cases()
        self.assertEqual(len(cases), len(self.document["cases"]))

    def test_missing_expected_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        del document["cases"][0]["expected"]
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            self.load(document)

    def test_unknown_error_type_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["cases"][1]["expected"] = {"ok": False, "errorType": "nope"}
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            self.load(document)

    def test_malformed_input_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        del document["cases"][0]["input"]["credential"]
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            self.load(document)

    def test_empty_cases_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            self.load({"cases": []})

    def test_duplicate_case_names_are_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["cases"].append(copy.deepcopy(document["cases"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate server verification case name"):
            self.load(document)


if __name__ == "__main__":
    unittest.main()
