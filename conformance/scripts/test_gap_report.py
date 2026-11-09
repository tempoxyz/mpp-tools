#!/usr/bin/env python3
"""Tests for the requirement gap report."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gap_report import (
    Claim,
    build_report,
    claim_applies,
    draft_short_name,
    extract_all,
    extract_requirements,
    load_claims,
    load_profiles,
    render_markdown,
)
from harness import validate_value

SPEC = """---
title: Example
docname: draft-tempo-charge-00
---

--- abstract

This document defines things.

--- middle

# Requirements Language

The key words "MUST", "MUST NOT", "SHOULD" and "MAY" in this document are to be
interpreted as described in BCP 14 {{RFC2119}} {{RFC8174}}.

# Challenge {#challenge}

Servers MUST include a
challenge.
{: #req-tempo-charge-001}

Clients SHOULD NOT retry.

- The `amount` MUST be a string.
  {: #req-tempo-charge-002}
- The `memo` MAY be omitted. {: #req-tempo-charge-003}
- Plain item without keywords.

| Field | Status |
|-------|--------|
| amount | REQUIRED |

~~~ json
{"note": "MUST be ignored inside code"}
~~~

<!-- A comment that MUST be ignored -->

## Second section

Servers MUST NOT store secrets.
"""


def write_specs(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "draft-tempo-charge-00.md"
        self.path.write_text(SPEC, encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_draft_short_names(self) -> None:
        self.assertEqual(draft_short_name("draft-httpauth-payment-01.md"), "core")
        self.assertEqual(draft_short_name("draft-tempo-charge-00.md"), "tempo-charge")
        self.assertEqual(
            draft_short_name("draft-payment-intent-charge-00.md"), "charge"
        )
        self.assertEqual(draft_short_name("draft-payment-discovery-01.md"), "discovery")

    def test_extracts_paragraphs_and_list_items(self) -> None:
        requirements, warnings, table_rows = extract_requirements(
            self.path, "specs/x.md"
        )

        self.assertEqual(warnings, [])
        self.assertEqual(table_rows, 1)
        self.assertEqual(
            [requirement.req_id for requirement in requirements],
            [
                "req-tempo-charge-001",
                None,
                "req-tempo-charge-002",
                "req-tempo-charge-003",
                None,
            ],
        )
        first, second, _, third, last = requirements
        self.assertEqual(first.text, "Servers MUST include a challenge.")
        self.assertEqual(first.section, "challenge")
        self.assertEqual(second.keywords, ["SHOULD NOT"])
        self.assertRegex(second.fallback_key, r"^tempo-charge#challenge:[0-9a-f]{8}$")
        self.assertEqual(third.text, "The `memo` MAY be omitted.")
        self.assertEqual(last.section, "second-section")
        self.assertEqual(last.keywords, ["MUST NOT"])

    def test_fallback_key_survives_reflow(self) -> None:
        before = extract_requirements(self.path, "specs/x.md")[0]
        self.path.write_text(
            SPEC.replace("Clients SHOULD NOT retry.", "Clients SHOULD NOT\nretry."),
            encoding="utf-8",
        )
        after = extract_requirements(self.path, "specs/x.md")[0]
        self.assertEqual(before[1].fallback_key, after[1].fallback_key)

    def test_warns_on_duplicate_and_foreign_ids(self) -> None:
        specs = write_specs(
            self.root / "checkout",
            {
                "specs/a/draft-tempo-charge-00.md": "# A\n\nX MUST hold.\n{: #req-tempo-charge-001}\n\n"
                "Y MUST hold.\n{: #req-tempo-charge-001}\n\nZ MAY hold.\n{: #req-core-001}\n",
            },
        )
        requirements, warnings, _ = extract_all(specs)
        self.assertEqual(len(requirements), 3)
        self.assertTrue(any("already used" in warning for warning in warnings))
        self.assertTrue(
            any(
                "req-core-001 does not use this draft's prefix" in warning
                for warning in warnings
            )
        )


class ReportTests(unittest.TestCase):
    def test_counts_coverage_unknown_ids_and_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft-tempo-charge-00.md"
            path.write_text(SPEC, encoding="utf-8")
            requirements, warnings, table_rows = extract_requirements(
                path, "specs/x.md"
            )
        claims = [
            Claim(
                "vectors/a.json#one",
                ["req-tempo-charge-001"],
                operation="challenge.parse",
            ),
            Claim(
                "vectors/a.json#two",
                ["req-tempo-charge-002", "req-tempo-charge-999"],
                adapters=["go"],
            ),
        ]
        profiles = {
            "python": {
                "profile": ["tempo-charge", "lightning-charge"],
                "capabilities": ["challenge.parse"],
            },
        }

        report = build_report(
            requirements,
            claims,
            profiles,
            warnings,
            {"source": "x", "commit": None},
            {"tempo-charge": table_rows},
        )

        draft = report["drafts"][0]
        self.assertEqual((draft["rules"], draft["tagged"], draft["covered"]), (5, 3, 2))
        self.assertEqual(draft["table_rows"], 1)
        self.assertEqual(len(draft["uncovered"]), 3)
        self.assertEqual(report["unknown_references"], ["req-tempo-charge-999"])
        self.assertEqual(
            report["profiles"]["python"]["drafts"]["tempo-charge"],
            {"rules": 5, "covered": 1},
        )
        self.assertEqual(
            report["profiles"]["python"]["unknown_drafts"], ["lightning-charge"]
        )
        markdown = render_markdown(report)
        self.assertIn("| tempo-charge | 5 | 3 | 2 | 1 |", markdown)
        self.assertIn("req-tempo-charge-999", markdown)

    def test_claim_applies(self) -> None:
        flow = Claim("flows/flows.json#x", ["req-core-001"], operation="http.flow")
        self.assertTrue(claim_applies(flow, "go", ["http.server"]))
        self.assertFalse(claim_applies(flow, "go", ["challenge.parse"]))
        vector = Claim(
            "vectors/a.json#x",
            ["req-core-001"],
            operation="challenge.parse",
            adapters=["rust"],
        )
        self.assertFalse(claim_applies(vector, "go", ["challenge.parse"]))
        self.assertTrue(claim_applies(vector, "rust", ["challenge.parse"]))


class FixtureTests(unittest.TestCase):
    def test_checked_in_fixtures_load(self) -> None:
        self.assertIsInstance(load_claims(), list)
        profiles = load_profiles()
        self.assertIn("core", profiles["go"]["profile"])

    def test_claims_from_vectors_and_flows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vectors = root / "vectors"
            vectors.mkdir()
            vector = {
                "version": "1.0.0",
                "description": "d",
                "spec_ref": "s",
                "commands": {"generate": "generate-challenge-id"},
                "scenarios": [
                    {
                        "name": "one",
                        "description": "d",
                        "tags": [],
                        "input": {},
                        "expected": "x",
                        "requirements": ["req-core-001"],
                    }
                ],
            }
            (vectors / "a.json").write_text(json.dumps(vector), encoding="utf-8")
            flows = root / "flows.json"
            flows.write_text(
                json.dumps(
                    {
                        "version": "1.0.0",
                        "cases": [
                            {
                                "name": "f",
                                "path": "/f",
                                "requirements": ["req-core-002"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            claims = load_claims(vectors, flows)

        self.assertEqual(
            [(claim.source, claim.requirements, claim.operation) for claim in claims],
            [
                ("vectors/a.json#one", ["req-core-001"], "challenge.id"),
                ("flows/flows.json#f", ["req-core-002"], "http.flow"),
            ],
        )

    def test_schemas_check_requirement_ids(self) -> None:
        case = {"name": "f", "path": "/f", "requirements": ["req-core-001"]}
        validate_value(
            {"version": "1.0.0", "cases": [case]}, "flow-cases.schema.json", "flows"
        )
        for bad in (["core-001"], ["req-core-1"], [], ["req-core-001", "req-core-001"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_value(
                    {"version": "1.0.0", "cases": [{**case, "requirements": bad}]},
                    "flow-cases.schema.json",
                    "flows",
                )


if __name__ == "__main__":
    unittest.main()
