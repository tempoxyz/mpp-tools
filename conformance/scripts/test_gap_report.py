#!/usr/bin/env python3
"""Tests for the requirement gap report."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
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
    git_commit,
    parse_heading,
    load_claims,
    load_profiles,
    main,
    render_markdown,
    version_checker,
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
            draft_short_name("draft-payment-intent-charge-00.md"), "intent-charge"
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

    def test_warns_on_detached_ids(self) -> None:
        self.path.write_text(
            "# A\n\nX MUST hold.\n\n{: #req-tempo-charge-001}\n\n{: #req-tempo-charge-002}\nY MAY hold.\n",
            encoding="utf-8",
        )
        requirements, warnings, _ = extract_requirements(self.path, "specs/x.md")
        self.assertEqual(
            [requirement.req_id for requirement in requirements], [None, None]
        )
        self.assertEqual(len(warnings), 2)
        self.assertIn(
            "req-tempo-charge-001 is not on the line right after a rule", warnings[0]
        )

    def test_front_matter_ending_with_section_marker(self) -> None:
        self.path.write_text(
            "---\ntitle: X\n--- abstract\n\nX MUST hold.\n\n--- middle\n\n# A\n\nY\n---\n\nZ MAY hold.\n",
            encoding="utf-8",
        )
        requirements, _, _ = extract_requirements(self.path, "specs/x.md")
        self.assertEqual(
            [requirement.text for requirement in requirements],
            ["X MUST hold.", "Z MAY hold."],
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

    def test_flow_claims_follow_case_mode(self) -> None:
        ordinary = Claim("flows/flows.json#a", ["req-core-001"], flow_mode="ordinary")
        client = Claim("flows/flows.json#b", ["req-core-001"], flow_mode="client_flow")
        server = Claim("flows/flows.json#c", ["req-core-001"], flow_mode="server_only")
        raw = Claim("flows/flows.json#d", ["req-core-001"], flow_mode="raw_http")
        self.assertFalse(claim_applies(raw, "typescript", ["http.payment_request"]))
        # Ruby and Java run ordinary flows without any http.* capability.
        self.assertTrue(claim_applies(ordinary, "java", ["challenge.parse"]))
        self.assertFalse(claim_applies(client, "java", ["challenge.parse"]))
        self.assertTrue(claim_applies(client, "go", ["http.payment_request"]))
        self.assertFalse(
            claim_applies(server, "go", ["http.payment_request", "http.server"])
        )

    def test_flow_modes_from_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            flows = Path(directory) / "flows.json"
            cases = [
                {"name": "a", "path": "/a", "requirements": ["req-core-001"]},
                {
                    "name": "b",
                    "path": "/b",
                    "client_flow": True,
                    "requirements": ["req-core-001"],
                },
                {
                    "name": "c",
                    "path": "/c",
                    "server_only": True,
                    "requirements": ["req-core-001"],
                },
                {
                    "name": "d",
                    "path": "/d",
                    "discovery": True,
                    "requirements": ["req-core-001"],
                },
            ]
            flows.write_text(
                json.dumps({"version": "1.0.0", "cases": cases}), encoding="utf-8"
            )
            vectors = Path(directory) / "vectors"
            vectors.mkdir()
            claims = load_claims(vectors, flows)
        self.assertEqual(
            [claim.flow_mode for claim in claims],
            ["ordinary", "client_flow", "server_only", "raw_http"],
        )

    def test_sdk_versions_use_runner_logic(self) -> None:
        warnings: list[str] = []
        versions = {"go": "0.4.0", "rust": "0.9.0"}
        applies = version_checker(warnings, resolver=lambda adapter: versions[adapter])
        claim = Claim(
            "vectors/a.json#x",
            ["req-core-001"],
            operation="challenge.parse",
            sdk_versions={"go": ">=0.5.0", "rust": ">=0.5.0"},
        )
        self.assertFalse(claim_applies(claim, "go", ["challenge.parse"], applies))
        self.assertTrue(claim_applies(claim, "rust", ["challenge.parse"], applies))
        self.assertTrue(claim_applies(claim, "python", ["challenge.parse"], applies))
        self.assertEqual(warnings, [])

    def test_unresolved_sdk_version_is_not_coverage(self) -> None:
        def missing(adapter: str) -> str:
            raise subprocess.CalledProcessError(1, ["cargo"])

        warnings: list[str] = []
        applies = version_checker(warnings, resolver=missing)
        claim = Claim(
            "vectors/a.json#x", ["req-core-001"], sdk_versions={"rust": ">=0.5.0"}
        )
        self.assertFalse(claim_applies(claim, "rust", [], applies))
        self.assertFalse(claim_applies(claim, "rust", [], applies))
        self.assertEqual(len(warnings), 1)
        self.assertIn("rust", warnings[0])

    def test_version_skipped_claims_do_not_count_for_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft-tempo-charge-00.md"
            path.write_text(SPEC, encoding="utf-8")
            requirements, warnings, _ = extract_requirements(path, "specs/x.md")
        claims = [
            Claim(
                "vectors/a.json#x",
                ["req-tempo-charge-001"],
                sdk_versions={"go": ">=9.0.0"},
            )
        ]
        profiles = {"go": {"profile": ["tempo-charge"], "capabilities": []}}
        report = build_report(
            requirements,
            claims,
            profiles,
            warnings,
            {"source": "x", "commit": None},
            version_applies=version_checker(warnings, resolver=lambda adapter: "1.0.0"),
        )
        self.assertEqual(
            report["profiles"]["go"]["drafts"]["tempo-charge"]["covered"], 0
        )
        self.assertEqual(report["drafts"][0]["covered"], 1)

    def test_claim_applies(self) -> None:
        vector = Claim(
            "vectors/a.json#x",
            ["req-core-001"],
            operation="challenge.parse",
            adapters=["rust"],
        )
        self.assertFalse(claim_applies(vector, "go", ["challenge.parse"]))
        self.assertTrue(claim_applies(vector, "rust", ["challenge.parse"]))


class SpecsPathTests(unittest.TestCase):
    def run_main(self, specs: Path, out: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "--specs",
                    str(specs),
                    "--json",
                    str(out / "r.json"),
                    "--markdown",
                    str(out / "r.md"),
                ]
            )
        return code, stderr.getvalue()

    def test_missing_specs_path_fails_without_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            code, stderr = self.run_main(out / "missing", out)
            self.assertEqual(code, 2)
            self.assertIn("does not exist", stderr)
            self.assertFalse((out / "r.json").exists())
            self.assertFalse((out / "r.md").exists())

    def test_checkout_without_drafts_fails_without_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "checkout" / "specs").mkdir(parents=True)
            code, stderr = self.run_main(out / "checkout", out)
            self.assertEqual(code, 2)
            self.assertIn("no specs/**/*.md drafts", stderr)
            self.assertFalse((out / "r.json").exists())

    def test_writes_report_for_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            write_specs(
                out / "checkout", {"specs/methods/tempo/draft-tempo-charge-00.md": SPEC}
            )
            code, _ = self.run_main(out / "checkout", out)
            self.assertEqual(code, 0)
            report = json.loads((out / "r.json").read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["rules"], 5)


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
            [
                (claim.source, claim.requirements, claim.operation or claim.flow_mode)
                for claim in claims
            ],
            [
                ("vectors/a.json#one", ["req-core-001"], "challenge.id"),
                ("flows/flows.json#f", ["req-core-002"], "ordinary"),
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


def test_git_commit_root_dirty_and_nested(tmp_path):
    import subprocess as sp

    repo = tmp_path / "specs"
    repo.mkdir()
    run = lambda *a: sp.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    (repo / "a.md").write_text("x\n")
    run("add", "a.md")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    head = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert git_commit(repo) == head
    (repo / "a.md").write_text("y\n")
    assert git_commit(repo) == f"{head}-dirty"
    nested = repo / "nested"
    nested.mkdir()
    assert git_commit(nested) is None


def test_parse_heading_is_linear_and_reads_anchors():
    import time

    assert parse_heading("## Extension Fields {#extension-fields}") == ("Extension Fields", "extension-fields")
    assert parse_heading("# Title  ") == ("Title", None)
    assert parse_heading("Not a heading") is None
    start = time.monotonic()
    parse_heading("# a" + " " * 20000 + "b" + " " * 20000 + "{")
    assert time.monotonic() - start < 1
