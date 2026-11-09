#!/usr/bin/env python3
"""Tests for the requirement gap report."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from gap_report import (
    Claim,
    Requirement,
    build_report,
    claim_applies,
    draft_paths,
    draft_short_name,
    extract_all,
    extract_requirements,
    flow_mode,
    git_commit,
    ial_req_id,
    parse_heading,
    load_claims,
    load_profiles,
    main,
    render_markdown,
    scenario_operations,
    version_checker,
)
from harness import VECTORS_DIR, validate_value

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
- {: #req-tempo-charge-003} The `memo` MAY be omitted.
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
        requirements, warnings, _, _ = extract_all(specs)
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

    def test_flow_claim_with_missing_challenge_path_covers_nothing(self) -> None:
        # flow_runner fails a case whose challenge_path no case's path matches before it
        # sends anything; a server_only case's path still counts as a match.
        with tempfile.TemporaryDirectory() as directory:
            flows = Path(directory) / "flows.json"
            cases = [
                {"name": "route", "path": "/route", "server_only": True},
                {
                    "name": "ok",
                    "path": "/ok",
                    "challenge_path": "/route",
                    "requirements": ["req-core-001"],
                },
                {
                    "name": "bad",
                    "path": "/bad",
                    "challenge_path": "/nowhere",
                    "requirements": ["req-core-002"],
                },
            ]
            flows.write_text(
                json.dumps({"version": "1.0.0", "cases": cases}), encoding="utf-8"
            )
            vectors = Path(directory) / "vectors"
            vectors.mkdir()
            claims = load_claims(vectors, flows)
        self.assertEqual(
            [(claim.source, claim.runner_error is None) for claim in claims],
            [("flows/flows.json#ok", True), ("flows/flows.json#bad", False)],
        )
        requirements = [
            Requirement(
                draft="draft-x-00", file="specs/x.md", line=line, section="",
                text="A MUST b.", req_id=f"req-core-00{line}", keywords=["MUST"],
            )
            for line in (1, 2)
        ]
        warnings: list[str] = []
        report = build_report(requirements, claims, {}, warnings, {}, adapters={})
        self.assertEqual(report["coverage"], {"req-core-001": ["flows/flows.json#ok"]})
        self.assertEqual(len(warnings), 1)
        self.assertIn("flows.json#bad: challenge_path /nowhere", warnings[0])

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
                {
                    "name": "e",
                    "path": "/e",
                    "client_flow": True,
                    "no_payment": True,
                    "requirements": ["req-core-001"],
                },
                {
                    "name": "f",
                    "path": "/f",
                    "no_payment": True,
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
            ["ordinary", "client_flow", "server_only", "raw_http", "client_flow", "raw_http"],
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

    def test_unknown_sdk_versions_keys_are_not_coverage(self) -> None:
        warnings: list[str] = []
        applies = version_checker(warnings, resolver=lambda adapter: "1.0.0")
        claim = Claim(
            "vectors/a.json#x", ["req-core-001"], sdk_versions={"golang": ">=0.5.0"}
        )
        # The runner refuses the scenario outright, so no SDK gets credit for it.
        self.assertFalse(claim_applies(claim, "go", [], applies))
        self.assertFalse(claim_applies(claim, "python", [], applies))
        self.assertEqual(len(warnings), 1)
        self.assertIn("golang", warnings[0])

    def test_runner_rejected_claims_are_not_suite_coverage(self) -> None:
        requirement = Requirement(
            draft="draft-x-00", file="specs/x.md", line=1, section="",
            text="A MUST b.", req_id="req-core-001", keywords=["MUST"],
        )
        claim = Claim(
            "vectors/a.json#x", ["req-core-001"], sdk_versions={"golang": ">=0.5.0"}
        )
        warnings: list[str] = []
        report = build_report([requirement], [claim], {}, warnings, {})
        self.assertEqual(report["totals"]["covered"], 0)
        self.assertEqual(report["coverage"], {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("golang", warnings[0])

    def test_claims_in_a_file_the_runner_refuses_are_not_coverage(self) -> None:
        source = json.loads((VECTORS_DIR / "tempo-proof.json").read_text(encoding="utf-8"))
        good = dict(source["scenarios"][0], name="good", requirements=["req-core-001"])
        bad = dict(source["scenarios"][0], name="bad", sdkVersions={"golang": ">=0.1.0"})
        source["scenarios"] = [good, bad]
        with tempfile.TemporaryDirectory() as directory:
            vectors = Path(directory) / "vectors"
            vectors.mkdir()
            (vectors / "tempo-proof.json").write_text(json.dumps(source), encoding="utf-8")
            flows = Path(directory) / "flows.json"
            flows.write_text(
                json.dumps({"version": "1.0.0", "cases": [{"name": "a", "path": "/a"}]}),
                encoding="utf-8",
            )
            claims = load_claims(vectors, flows)
        self.assertEqual([claim.source for claim in claims], ["vectors/tempo-proof.json#good"])
        self.assertIn("golang", claims[0].runner_error or "")
        requirement = Requirement(
            draft="draft-x-00", file="specs/x.md", line=1, section="",
            text="A MUST b.", req_id="req-core-001", keywords=["MUST"],
        )
        warnings: list[str] = []
        report = build_report([requirement], claims, {}, warnings, {})
        self.assertEqual(report["totals"]["covered"], 0)
        self.assertEqual(len(warnings), 1)

    def test_malformed_sdk_version_constraint_is_not_coverage(self) -> None:
        warnings: list[str] = []
        applies = version_checker(warnings, resolver=lambda adapter: "1.0.0")
        claim = Claim("vectors/a.json#x", ["req-core-001"], sdk_versions={"go": "=>1.0.0"})
        # The runner parses go's expression only when it runs the file for go, and raises
        # there, aborting the file for go alone; other adapters still run the scenario.
        self.assertFalse(claim_applies(claim, "go", [], applies))
        self.assertTrue(claim_applies(claim, "python", [], applies))
        self.assertEqual(len(warnings), 1)
        self.assertIn("=>1.0.0", warnings[0])

    def extract(self, text: str) -> tuple[list, int]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft-tempo-charge-00.md"
            path.write_text(text, encoding="utf-8")
            requirements, _, rows = extract_requirements(path, "specs/x.md")
        return requirements, rows

    def test_extraction_follows_kramdown_rfc_blocks(self) -> None:
        bt = "\x60" * 3
        cases = {
            # indented code is code, not a rule
            "Intro.\n\n    code MUST x\n\nP MUST y.\n": 1,
            # a bare '>' separates quoted paragraphs; quoted list items are their own blocks
            "> A MUST a.\n>\n> B MUST b.\n": 2,
            "> - A MUST a.\n> - B MUST b.\n": 2,
            # a backtick pair is inline verbatim, not a fence and not a rule
            f"{bt}\ncode MUST\n{bt}\n\nP MUST y.\n": 1,
            # a ~~~ indented 4 does not close the block
            "~~~\n    ~~~\nMUST in code\n~~~\n\nP MUST y.\n": 1,
            # tab-indented text is indented code
            "Intro.\n\n\tcode MUST x\n\nP MUST y.\n": 1,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(len(self.extract(text)[0]), expected)

    def test_table_without_leading_pipe_counts_rows(self) -> None:
        requirements, rows = self.extract("a | b\n--- | ---\nx | REQUIRED\ny | OPTIONAL\n\nP MUST y.\n")
        self.assertEqual(len(requirements), 1)
        self.assertEqual(rows, 2)

    def test_symlinked_drafts_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "specs").mkdir()
            (root / "specs/draft-a-00.md").write_text("A MUST b.\n", encoding="utf-8")
            (root / "outside.md").write_text("Secret MUST x.\n", encoding="utf-8")
            (root / "specs/draft-b-00.md").symlink_to(root / "outside.md")
            self.assertEqual([p.name for p in draft_paths(root)], ["draft-a-00.md"])

    def test_parse_format_scenarios_need_each_tested_command(self) -> None:
        vector = {"commands": {"parse": "parse-www-authenticate", "format": "format-www-authenticate"}}
        # A format test expecting success compares semantically, parsing both wire values.
        self.assertEqual(
            scenario_operations(vector, {"tests": {"format": True}}),
            ("challenge.parse", "challenge.format"),
        )
        self.assertEqual(
            scenario_operations(vector, {"tests": {"format": "exact"}}), ("challenge.format",)
        )
        self.assertEqual(
            scenario_operations(vector, {"tests": {"roundtrip": True}}),
            ("challenge.parse", "challenge.format"),
        )
        claim = Claim("vectors/a.json#x", ["req-core-001"], operation="challenge.parse",
                      operations=("challenge.format",))
        self.assertFalse(claim_applies(claim, "go", ["challenge.parse"]))
        self.assertTrue(claim_applies(claim, "go", ["challenge.parse", "challenge.format"]))

    def test_unresolved_version_voids_files_that_pin_the_adapter(self) -> None:
        def missing(adapter: str) -> str:
            raise subprocess.CalledProcessError(1, ["cargo"])

        warnings: list[str] = []
        applies = version_checker(warnings, resolver=missing)
        # This scenario has no constraint itself, but a sibling pins rust: the runner
        # resolves rust's version for the file and aborts it for rust when it can't.
        claim = Claim(
            "vectors/a.json#x", ["req-core-001"], file_constraints={"rust": (">=0.1.0",)}
        )
        self.assertFalse(claim_applies(claim, "rust", [], applies))
        self.assertTrue(claim_applies(claim, "python", [], applies))
        self.assertEqual(len(warnings), 1)

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
            self.assertIn("no specs/**/draft-*.md drafts", stderr)
            self.assertFalse((out / "r.json").exists())
            # Markdown that is not a draft (a README) does not make it a specs checkout.
            (out / "checkout" / "specs" / "README.md").write_text("Notes MUST x.\n")
            code, stderr = self.run_main(out / "checkout", out)
            self.assertEqual(code, 2)
            self.assertIn("no specs/**/draft-*.md drafts", stderr)
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


class HelperTests(unittest.TestCase):
    def test_duplicate_draft_names_keep_highest_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = Path(directory) / "specs" / "methods"
            specs.mkdir(parents=True)
            body = "---\ntitle: T\n---\n\n--- middle\n\nClients MUST do {0}.\n"
            (specs / "draft-x-charge-00.md").write_text(body.format("old"))
            (specs / "draft-x-charge-01.md").write_text(body.format("new"))
            requirements, warnings, _, _ = extract_all(Path(directory))
        self.assertEqual([r.file for r in requirements], ["specs/methods/draft-x-charge-01.md"])
        self.assertTrue(any("skipped" in w for w in warnings))

    def test_git_commit_root_dirty_and_nested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "specs"
            repo.mkdir()

            def run(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            run("init", "-q")
            draft = repo / "specs" / "a.md"
            draft.parent.mkdir()
            draft.write_text("x\n")
            run("add", "specs/a.md")
            run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
            head = run("rev-parse", "HEAD")
            self.assertEqual(git_commit(repo), head)
            # Untracked files outside specs/ don't change the counted rules.
            (repo / "notes.txt").write_text("n\n")
            self.assertEqual(git_commit(repo), head)
            # A gitignored draft is read by discovery too.
            (repo / ".gitignore").write_text("specs/ignored-*.md\n")
            run("add", ".gitignore")
            run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore")
            head = run("rev-parse", "HEAD")
            self.assertEqual(git_commit(repo), head)
            (repo / "specs" / "ignored-c.md").write_text("hidden\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")
            (repo / "specs" / "ignored-c.md").unlink()
            # An ignored directory without drafts leaves the checkout clean.
            (repo / ".git" / "info" / "exclude").write_text("specs/build/\n")
            (repo / "specs" / "build").mkdir()
            (repo / "specs" / "build" / "out.html").write_text("<p>\n")
            self.assertEqual(git_commit(repo), head)
            (repo / "specs" / "build" / "draft.md").write_text("hidden\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")
            (repo / "specs" / "build" / "draft.md").unlink()
            # A draft whose name git status would quote ("a b.md") is still seen.
            (repo / "specs" / "a b.md").write_text("spaced\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")
            (repo / "specs" / "a b.md").unlink()
            # A draft inside a nested repository (invisible to git status) is seen too.
            nested_repo = repo / "specs" / "vendor"
            nested_repo.mkdir()
            subprocess.run(["git", "init", "-q", str(nested_repo)], check=True)
            (nested_repo / "x.md").write_text("vendored\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")
            shutil.rmtree(nested_repo)
            self.assertEqual(git_commit(repo), head)
            # An untracked draft is read by discovery, so the checkout is dirty.
            (repo / "specs" / "b.md").write_text("new\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")
            (repo / "specs" / "b.md").unlink()
            draft.write_text("y\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")
            nested = repo / "nested"
            nested.mkdir()
            self.assertIsNone(git_commit(nested))

    def test_parse_heading_is_linear_and_reads_anchors(self) -> None:
        self.assertEqual(
            parse_heading("## Extension Fields {#extension-fields}"),
            ("Extension Fields", "extension-fields"),
        )
        self.assertEqual(parse_heading("# Title  "), ("Title", None))
        self.assertIsNone(parse_heading("Not a heading"))
        start = time.monotonic()
        parse_heading("# a" + " " * 20000 + "b" + " " * 20000 + "{")
        self.assertLess(time.monotonic() - start, 1)


# kramdown-rfc output for each case below was checked with kramdown-rfc 1.7.39
# (docker image ietf-spec-tools); the expected rules mirror what it renders as prose.
HEADER = "---\ntitle: T\ndocname: draft-tempo-charge-00\n---\n\n--- abstract\n\nAbs.\n\n--- middle\n\n# A\n\n"


def read_draft(body: str, header: str = HEADER) -> tuple[list[Requirement], list[str], int]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "draft-tempo-charge-00.md"
        path.write_text(header + body, encoding="utf-8")
        return extract_requirements(path, "specs/x.md")


def rules_in(body: str) -> list[Requirement]:
    """The rules in body, leaving out the abstract's own paragraph (which has none)."""
    return read_draft(body)[0]


class KramdownReadingTests(unittest.TestCase):
    def test_wrapped_two_word_keywords_keep_their_strength(self) -> None:
        first, second = rules_in(
            "Clients MUST\nNOT retry.\n\nIt is NOT\nRECOMMENDED; servers SHOULD\nNOT x.\n"
        )
        self.assertEqual(first.keywords, ["MUST NOT"])
        self.assertEqual(second.keywords, ["NOT RECOMMENDED", "SHOULD NOT"])

    def test_comments_are_removed_where_they_open_and_close(self) -> None:
        # A comment line inside a paragraph stays inside it, so the IAL still tags it.
        (rule,) = rules_in(
            "P MUST three\n<!-- hidden MAY x -->\ncontinues here.\n{: #req-tempo-charge-003}\n"
        )
        self.assertEqual(
            (rule.text, rule.keywords, rule.req_id),
            ("P MUST three continues here.", ["MUST"], "req-tempo-charge-003"),
        )
        cases = {
            # text after a comment on the same line is prose
            "<!-- c --> Trailing SHALL four.\n": [["SHALL"]],
            "<!-- open\nmore -->Tail MUST x.\n": [["MUST"]],
            # a comment that opens mid-line hides its keywords
            "Mid <!-- inline OPTIONAL --> text REQUIRED five.\n": [["REQUIRED"]],
            # kramdown {::comment} blocks and spans
            "{::comment}\nCommented MUST NOT six.\n{:/comment}\n\n"
            "Inline {::comment}MAY{:/comment} plain SHOULD.\n": [["SHOULD"]],
            "{::comment}\nGone MUST.\n{:/}\n": [],
            # a '<!--' in a code span opens no comment
            "Quoted \x60<!--\x60 MUST stay.\n\nNext MAY.\n": [["MUST"], ["MAY"]],
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual([r.keywords for r in rules_in(body)], expected)

    def test_ids_come_only_from_real_ial_id_tokens(self) -> None:
        cases = {
            # an escaped brace is literal text
            "Escaped MUST eight. \\{: #req-tempo-charge-008}\n": None,
            # a quoted value is not an ID
            'Title MUST nine.\n{: title="#req-tempo-charge-009"}\n': None,
            # kramdown keeps the last #id
            "P MUST a.\n{: #req-tempo-charge-001 .x #req-tempo-charge-002}\n": "req-tempo-charge-002",
            # an IAL at the start of a list item's text tags the item
            "- {: #req-tempo-charge-005} Item MUST x.\n": "req-tempo-charge-005",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                requirements, warnings, _ = read_draft(body)
                self.assertEqual([r.req_id for r in requirements], [expected])
                self.assertEqual(warnings, [])
        (item,) = rules_in("- {: #req-tempo-charge-005} Item MUST x.\n")
        self.assertEqual(item.text, "Item MUST x.")
        # At the end of a text line kramdown-rfc prints the IAL: no ID, and a warning.
        requirements, warnings, _ = read_draft("Para MUST x. {: #req-tempo-charge-003}\n")
        self.assertEqual([r.req_id for r in requirements], [None])
        self.assertEqual(len(warnings), 1)
        self.assertIn("put it on the next line", warnings[0])
        # Extension tags such as {::boilerplate} set no ID and draw no warning.
        requirements, warnings, _ = read_draft("{::boilerplate bcp14-tagged}\n\nP MUST.\n")
        self.assertEqual((len(requirements), warnings), (1, []))

    def test_keywords_in_code_spans_and_links_are_ignored(self) -> None:
        cases = {
            # a double-backtick span may hold a backtick
            "Code \x60\x60a \x60 MUST\x60\x60 and MAY b.\n": [["MAY"]],
            # an unmatched backtick is literal text, so the MUST after it is prose
            "A \x60 MUST b and \x60c\x60 MAY.\n": [["MAY", "MUST"]],
            "Link [x](https://e.com/MUST) ten MAY.\n": [["MAY"]],
            "Only [x](https://e.com/MUST \"MUST\") and <https://e.com/MUST>.\n": [],
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual([r.keywords for r in rules_in(body)], expected)

    def test_quoted_keywords_match_the_specs_linter(self) -> None:
        # As mpp-specs' lint_requirements.py: only a keyword double-quoted on its own is
        # dropped; one inside a longer quoted phrase or in single quotes is a rule.
        cases = {
            'The key words "MUST", "MUST NOT" and "MAY" are\n"SHOULD\nNOT".\n': [],
            'Text says "clients MUST retry".\n': [["MUST"]],
            'Text says "MUST retry" or "retry MAY".\n': [["MAY", "MUST"]],
            "Text says 'MUST'.\n": [["MUST"]],
            # a digit next to it makes a different word, as in the linter
            "Field MUST2 and 3MAY.\n": [],
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual([r.keywords for r in rules_in(body)], expected)

    def test_tables_follow_kramdown(self) -> None:
        # body: (rules, keyword table rows)
        cases = {
            # a pipe on a block's first line makes a table, with no leading pipe needed
            "Value MUST be a|b here.\n\nP MAY.\n": (1, 1),
            "a | b\nx | REQUIRED\n\nafter MAY.\n": (1, 1),
            # but not inside a code span, or when escaped
            "Value MUST be \x60a|b\x60 here.\n": (1, 0),
            "Value MUST be a\\|b here.\n": (1, 0),
            # pipe lines continuing a paragraph stay in it
            "Para MUST a\n| a | b |\n|---|---|\n| c | REQUIRED |\n": (1, 0),
            # rows not followed by a block boundary are a paragraph after all
            "| a | b |\n| c | REQUIRED |\nAfter MUST x.\n": (1, 0),
            # a list item's text may itself be a table
            "- item MUST a | b.\n- other MAY\n": (1, 1),
            # a table ending its section still counts
            "| a | REQUIRED |\n": (0, 1),
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                requirements, _, rows = read_draft(body)
                self.assertEqual((len(requirements), rows), expected)

    def test_sections_follow_kramdown_rfc(self) -> None:
        requirements = rules_in(
            "Mid MUST.\n\n---\n\nAfter MAY.\n\n--- back\n\nBack SHOULD.\n\n--- fluff\n\nFluff MUST.\n"
        )
        # A bare '---' pops back to the abstract, which renders; 'fluff' never does.
        self.assertEqual(
            [r.text for r in requirements], ["Mid MUST.", "After MAY.", "Back SHOULD."]
        )
        # Text between the front matter's closing '---' and the first named section
        # belongs to no section, so kramdown-rfc drops it.
        requirements, _, _ = read_draft(
            "", header="---\ntitle: T\n---\n\nLoose MUST.\n\n--- middle\n\nKept MUST.\n"
        )
        self.assertEqual([r.text for r in requirements], ["Kept MUST."])


class CoverageCorrectnessTests(unittest.TestCase):
    def test_duplicate_and_foreign_ids_are_never_covered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = write_specs(
                Path(directory),
                {
                    "specs/a/draft-tempo-charge-00.md": "# A\n\nX MUST hold.\n{: #req-tempo-charge-001}\n\n"
                    "Y MUST hold.\n{: #req-tempo-charge-001}\n\nZ MAY hold.\n{: #req-core-001}\n\n"
                    "W MUST hold.\n{: #req-tempo-charge-002}\n",
                },
            )
            requirements, warnings, table_rows, files = extract_all(specs)
        claims = [
            Claim("vectors/a.json#x", ["req-tempo-charge-001", "req-core-001", "req-tempo-charge-002"])
        ]
        profiles = {"go": {"profile": ["tempo-charge"], "capabilities": []}}
        report = build_report(
            requirements, claims, profiles, warnings, {}, table_rows, draft_files=files
        )
        (draft,) = report["drafts"]
        # Only W's ID is its own: one claim on a shared or foreign ID covers nothing.
        self.assertEqual((draft["rules"], draft["tagged"], draft["covered"]), (4, 4, 1))
        self.assertEqual(report["profiles"]["go"]["drafts"]["tempo-charge"]["covered"], 1)
        self.assertEqual(
            sorted(rule.get("id_problem") for rule in draft["uncovered"]),
            ["duplicate", "duplicate", "foreign"],
        )
        self.assertIn("[duplicate ID, not credited]", render_markdown(report))

    def test_drafts_without_prose_rules_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = write_specs(
                Path(directory),
                {
                    "specs/a/draft-tempo-charge-00.md": "# A\n\n| Field | Status |\n|---|---|\n| a | REQUIRED |\n",
                    "specs/b/draft-httpauth-payment-00.md": "# B\n\nX MUST hold.\n",
                },
            )
            requirements, warnings, table_rows, files = extract_all(specs)
        profiles = {"go": {"profile": ["tempo-charge"], "capabilities": []}}
        report = build_report(
            requirements, [], profiles, warnings, {}, table_rows, draft_files=files
        )
        entries = {entry["draft"]: entry for entry in report["drafts"]}
        self.assertEqual((entries["tempo-charge"]["rules"], entries["tempo-charge"]["table_rows"]), (0, 1))
        self.assertEqual(report["totals"]["table_rows"], 1)
        self.assertEqual(report["profiles"]["go"]["unknown_drafts"], [])
        self.assertEqual(report["profiles"]["go"]["drafts"]["tempo-charge"], {"rules": 0, "covered": 0})

    def load_vector_claims(self, vector: dict) -> list[Claim]:
        with tempfile.TemporaryDirectory() as directory:
            vectors = Path(directory) / "vectors"
            vectors.mkdir()
            (vectors / "v.json").write_text(json.dumps(vector), encoding="utf-8")
            flows = Path(directory) / "flows.json"
            flows.write_text(
                json.dumps({"version": "1.0.0", "cases": [{"name": "a", "path": "/a"}]}),
                encoding="utf-8",
            )
            return load_claims(vectors, flows)

    def www_authenticate_vector(self, **commands: str) -> dict:
        vector = json.loads((VECTORS_DIR / "www-authenticate.json").read_text(encoding="utf-8"))
        scenario = {
            key: value
            for key, value in vector["scenarios"][0].items()
            if key not in ("adapters", "sdkVersions")
        }
        scenario.update(name="tagged", tests={"parse": True}, requirements=["req-core-001"])
        vector["commands"] = commands or vector["commands"]
        vector["scenarios"] = [scenario]
        return vector

    def test_unmapped_legacy_commands_credit_nothing(self) -> None:
        (claim,) = self.load_vector_claims(self.www_authenticate_vector(parse="parse-nonsense"))
        self.assertIn("maps to no adapter operation", claim.runner_error or "")
        requirement = Requirement(
            draft="core", file="specs/x.md", line=1, section="",
            text="A MUST b.", req_id="req-core-001", keywords=["MUST"],
        )
        warnings: list[str] = []
        profiles = {"go": {"profile": ["core"], "capabilities": ["challenge.parse"]}}
        report = build_report([requirement], [claim], profiles, warnings, {})
        self.assertEqual(report["totals"]["covered"], 0)
        self.assertEqual(report["profiles"]["go"]["drafts"]["core"]["covered"], 0)

    def test_version_resolution_follows_sibling_adapter_selection(self) -> None:
        vector = self.www_authenticate_vector()
        sibling = dict(
            vector["scenarios"][0], name="sibling", adapters=["go"],
            sdkVersions={"rust": ">=1.0.0", "go": ">=0.1.0"},
        )
        del sibling["requirements"]
        vector["scenarios"].append(sibling)
        (claim,) = self.load_vector_claims(vector)
        # The runner skips the sibling for rust before it looks at its versions.
        self.assertEqual(claim.file_constraints, {"go": (">=0.1.0",)})

        def missing(adapter: str) -> str:
            raise subprocess.CalledProcessError(1, ["cargo"])

        applies = version_checker([], resolver=missing)
        self.assertTrue(claim_applies(claim, "rust", ["challenge.parse"], applies))
        self.assertFalse(claim_applies(claim, "go", ["challenge.parse"], applies))

    def test_sibling_constraints_only_need_to_resolve_and_parse(self) -> None:
        # A prerelease SDK fails a sibling's range, but only that sibling is skipped.
        applies = version_checker([], resolver=lambda adapter: "1.0.0-beta.1")
        claim = Claim(
            "vectors/a.json#x", ["req-core-001"], file_constraints={"rust": (">=2.0.0",)}
        )
        self.assertTrue(claim_applies(claim, "rust", [], applies))

    def test_failing_resolver_runs_once_per_adapter(self) -> None:
        calls: list[str] = []

        def missing(adapter: str) -> str:
            calls.append(adapter)
            raise subprocess.CalledProcessError(1, ["cargo"])

        warnings: list[str] = []
        applies = version_checker(warnings, resolver=missing)
        for name in ("x", "y", "z"):
            claim = Claim(f"vectors/{name}.json#x", ["req-core-001"], sdk_versions={"rust": ">=0.1.0"})
            self.assertFalse(claim_applies(claim, "rust", [], applies))
        self.assertEqual((calls, len(warnings)), (["rust"], 1))

    def test_git_commit_ignores_eol_conversion_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "specs-repo"
            repo.mkdir()

            def run(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()

            run("init", "-q")
            run("config", "core.autocrlf", "true")
            (repo / "specs").mkdir()
            (repo / "specs" / "a.md").write_bytes(b"x\r\ny\r\n")
            (repo / "specs" / "link.md").symlink_to("a.md")
            run("add", "-A")
            run("commit", "-qm", "init")
            head = run("rev-parse", "HEAD")
            # The blob holds LF endings and the working file CRLF: still clean.
            self.assertEqual(git_commit(repo), head)
            (repo / "specs" / "a.md").write_bytes(b"x\r\nchanged\r\n")
            self.assertEqual(git_commit(repo), f"{head}-dirty")


class CyclopsFindingTests(unittest.TestCase):
    """Regression tests for the Cyclops findings on 9f14fe8 (kramdown cases checked
    with kramdown-rfc 1.7.39 in the ietf-spec-tools image)."""

    def test_unclosed_quoted_ial_value_with_backslashes_is_linear(self) -> None:
        # Each backslash used to match either alone or as half of an escape pair, so an
        # unclosed value with n backslashes backtracked through ~1.7^n splits (40 took
        # most of a minute). Now there is one way to match each character.
        for body in (
            'title="' + "\\" * 40 + '"x',
            'title="' + "\\" * 40 + "x",
            "title='" + "\\" * 100000 + "'x #req-tempo-charge-001",
        ):
            start = time.monotonic()
            ial_req_id(body)
            self.assertLess(time.monotonic() - start, 1)
        # Escapes still read as kramdown-rfc reads them.
        self.assertEqual(ial_req_id('title="a\\" #req-tempo-charge-010'), "req-tempo-charge-010")
        self.assertIsNone(ial_req_id('title="a\\" #req-tempo-charge-011" '))
        self.assertEqual(ial_req_id('title="a\\\\" #req-tempo-charge-012'), "req-tempo-charge-012")

    def test_nested_blockquote_ials_tag_their_paragraph(self) -> None:
        for body in (
            "> > Servers MUST nest.\n> > {: #req-tempo-charge-001}\n",
            ">> Servers MUST nest.\n>> {: #req-tempo-charge-001}\n",
            "> Servers MUST nest.\n> {: #req-tempo-charge-001}\n",
        ):
            with self.subTest(body=body):
                requirements, warnings, _ = read_draft(body)
                self.assertEqual(
                    [(r.text, r.req_id) for r in requirements],
                    [("Servers MUST nest.", "req-tempo-charge-001")],
                )
                self.assertEqual(warnings, [])

    def test_comments_in_later_table_rows_hide_their_keywords(self) -> None:
        # body: (rule keywords, keyword table rows)
        cases = {
            "| a | b |\n|---|---|\n| x <!-- MUST --> | y |\n"
            "| z {::comment}SHOULD{:/comment} | w |\n\nAfter.\n": ([], 0),
            "| a | b |\n|---|---|\n| x <!-- MUST --> | REQUIRED |\n\nAfter.\n": ([], 1),
            # a comment a row leaves open hides the lines it runs over
            "| a | b |\n| x <!-- open | y |\nMUST hidden -->\n\nAfter.\n": ([], 0),
            # rows that fall back to a paragraph keep only their visible text
            "| a | b |\n| c <!-- MUST --> | d |\nplain SHOULD.\n": ([["SHOULD"]], 0),
        }
        for body, (keywords, rows) in cases.items():
            with self.subTest(body=body):
                requirements, _, table_rows = read_draft(body)
                self.assertEqual(([r.keywords for r in requirements], table_rows), (keywords, rows))

    def test_vector_claims_no_adapter_runs_are_not_suite_coverage(self) -> None:
        requirements = [
            Requirement(
                draft="core", file="specs/x.md", line=n, section="",
                text=f"A MUST {n}.", req_id=f"req-core-00{n}", keywords=["MUST"],
            )
            for n in range(1, 6)
        ]
        adapters = {"go": ["challenge.parse"], "rust": ["challenge.parse", "challenge.format"]}
        claims = [
            # run by go and rust
            Claim("vectors/a.json#run", ["req-core-001"], operation="challenge.parse"),
            # no adapter advertises the operation
            Claim("vectors/a.json#op", ["req-core-002"], operation="receipt.parse"),
            # the one adapter selected lacks the operation
            Claim("vectors/a.json#sel", ["req-core-003"], operation="challenge.format", adapters=["go"]),
            # sdkVersions rule out the only adapter with the operation
            Claim(
                "vectors/a.json#ver", ["req-core-004"], operation="challenge.format",
                sdk_versions={"rust": ">=9.0.0"},
            ),
            # raw_http flow cases check the reference server without an adapter
            Claim("flows/flows.json#r", ["req-core-005"], flow_mode="raw_http"),
            # server_only flow cases are never run by flow_runner, so they cover nothing
            Claim("flows/flows.json#s", ["req-core-006"], flow_mode="server_only"),
        ]
        warnings: list[str] = []
        applies = version_checker(warnings, resolver=lambda adapter: "1.0.0")
        report = build_report(
            requirements, claims, {}, warnings, {}, version_applies=applies, adapters=adapters
        )
        self.assertEqual(sorted(report["coverage"]), ["req-core-001", "req-core-005"])
        self.assertEqual(report["totals"]["covered"], 2)
        self.assertEqual(
            sorted(w.split(":")[0] for w in warnings if "no adapter runs it" in w),
            ["vectors/a.json#op", "vectors/a.json#sel", "vectors/a.json#ver"],
        )
        self.assertTrue(any(w.startswith("flows/flows.json#s: server_only") for w in warnings))

    def test_unknown_ids_reported_on_claims_that_cover_nothing(self) -> None:
        requirements = [
            Requirement("core", "draft-core.md", "x", 1, "MUST", "req-core-001", None),
        ]
        claims = [
            # never run, so it covers nothing, but its unknown ID must still be reported
            Claim("flows/flows.json#s", ["req-core-998"], flow_mode="server_only"),
            # no adapter advertises the operation
            Claim("vectors/a.json#op", ["req-core-999"], operation="receipt.parse"),
        ]
        warnings: list[str] = []
        report = build_report(
            requirements, claims, {}, warnings, {}, adapters={"go": ["challenge.parse"]}
        )
        self.assertEqual(report["coverage"], {})
        self.assertEqual(report["unknown_references"], ["req-core-998", "req-core-999"])
        self.assertEqual(
            report["unknown_reference_sources"],
            {"req-core-998": ["flows/flows.json#s"], "req-core-999": ["vectors/a.json#op"]},
        )
        markdown = render_markdown(report)
        self.assertIn("req-core-998\x60: flows/flows.json#s", markdown)

class FlowModeForcedStatusTest(unittest.TestCase):
    def test_forced_non_402_status_is_server_only_coverage(self) -> None:
        # flow_runner stops at the first non-402 response, before any adapter call.
        self.assertEqual(flow_mode({"name": "e", "path": "/e", "force_status": 500}), "raw_http")
        self.assertEqual(flow_mode({"name": "p", "path": "/p", "force_status": 402}), "ordinary")
        self.assertEqual(
            flow_mode({"name": "c", "path": "/c", "force_status": 500, "client_flow": True}),
            "client_flow",
        )


class GenerateCommitStabilityTest(unittest.TestCase):
    def test_checkout_moving_while_read_fails(self) -> None:
        import gap_report

        commits = iter(["a" * 40, "b" * 40])
        with (
            unittest.mock.patch.object(gap_report, "git_commit", lambda _p: next(commits)),
            unittest.mock.patch.object(gap_report, "specs_fingerprint", lambda _p: "f"),
            unittest.mock.patch.object(
                gap_report, "extract_all", lambda _p: ([], [], {}, {})
            ),
            self.assertRaisesRegex(ValueError, "changed while it was read"),
        ):
            gap_report.generate(Path("."), "x")


class ReviewFindingTests(unittest.TestCase):
    """Regression tests for review findings; kramdown behavior checked with kramdown-rfc 1.7.39."""

    def test_character_references_render_as_keywords(self) -> None:
        cases = {
            "Clients &#77;UST retry.\n": [["MUST"]],
            "Servers &#x4d;UST NOT drop.\n": [["MUST NOT"]],
            "Peers &#x4D;AY and &#0077;UST go.\n": [["MAY", "MUST"]],
            # kramdown needs the ';' and a lowercase 'x': these stay literal text
            "A &#77UST b and &#X4d;UST c.\n": [],
            # inside a code span a reference is verbatim
            "Code \x60&#77;UST\x60 here.\n": [],
            # &quot; renders as '"', so this is the quoted keyword of the boilerplate
            "The key words &quot;MUST&quot; are.\n": [],
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual([r.keywords for r in rules_in(body)], expected)

    def test_final_ial_id_decides_the_requirement_id(self) -> None:
        cases = {
            'id="req-tempo-charge-001"': "req-tempo-charge-001",
            "id='req-tempo-charge-002'": "req-tempo-charge-002",
            # the last ID wins, whether a #name or an id= value
            'id="note" #req-tempo-charge-003': "req-tempo-charge-003",
            '#req-tempo-charge-004 id="note"': None,
            "#req-tempo-charge-005 #note": None,
            # another attribute's value, or an unquoted id=, sets no ID
            'title="#req-tempo-charge-006"': None,
            "id=req-tempo-charge-007": None,
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(ial_req_id(body), expected)
        cases = {
            'P MUST a.\n{: id="req-tempo-charge-008"}\n': "req-tempo-charge-008",
            'P MUST a.\n{: #req-tempo-charge-009 id="note"}\n': None,
            # kramdown puts a block IAL after a list item on the list; the item keeps the
            # ID at the start of its text
            "- {: #req-tempo-charge-010} Item MUST x.\n{: #note}\n": "req-tempo-charge-010",
            '- {: #req-tempo-charge-011} Item MUST x.\n{: id="note"}\n': "req-tempo-charge-011",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                requirements, warnings, _ = read_draft(body)
                self.assertEqual([r.req_id for r in requirements], [expected])
                self.assertEqual(warnings, [])

    def test_malformed_ids_are_reported_but_not_tagged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = write_specs(
                Path(directory),
                {
                    "specs/a/draft-tempo-charge-00.md": "# A\n\nX MUST hold.\n{: #req-tempo-charge-01}\n\n"
                    "W MUST hold.\n{: #req-tempo-charge-002}\n",
                },
            )
            requirements, warnings, table_rows, files = extract_all(specs)
        claims = [Claim("vectors/a.json#x", ["req-tempo-charge-01", "req-tempo-charge-002"])]
        profiles = {"go": {"profile": ["tempo-charge"], "capabilities": []}}
        report = build_report(
            requirements, claims, profiles, warnings, {}, table_rows, draft_files=files
        )
        (draft,) = report["drafts"]
        self.assertEqual((draft["rules"], draft["tagged"], draft["covered"]), (2, 1, 1))
        self.assertEqual(report["totals"]["tagged"], 1)
        (uncovered,) = draft["uncovered"]
        self.assertEqual(
            (uncovered["key"], uncovered["id_problem"]), ("req-tempo-charge-01", "malformed")
        )
        self.assertEqual(report["unknown_references"], ["req-tempo-charge-01"])
        self.assertTrue(any("does not match req-<draft>-<nnn>" in w for w in warnings))
        self.assertIn("[malformed ID, not credited]", render_markdown(report))

    def test_dirty_checkout_changing_again_while_read_fails(self) -> None:
        import gap_report

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            draft = repo / "specs" / "draft-tempo-charge-00.md"
            write_specs(repo, {"specs/draft-tempo-charge-00.md": "# A\n\nX MUST hold.\n"})
            for args in (
                ["init", "-q"],
                ["add", "."],
                ["-c", "user.name=t", "-c", "user.email=t@e", "-c", "commit.gpgsign=false",
                 "commit", "-qm", "init"],
            ):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            draft.write_text("# A\n\nX MUST hold.\n\nY MAY hold.\n", encoding="utf-8")
            label = git_commit(repo)
            self.assertTrue(label and label.endswith("-dirty"))
            extract = gap_report.extract_all

            def extract_then_edit(path: Path):  # type: ignore[no-untyped-def]
                result = extract(path)
                draft.write_text("# A\n\nZ SHOULD hold.\n", encoding="utf-8")
                return result

            with (
                unittest.mock.patch.object(gap_report, "extract_all", extract_then_edit),
                self.assertRaisesRegex(ValueError, "drafts changed while they were read"),
            ):
                gap_report.generate(repo, "x")
            # Both samples carry the same '-dirty' label: only the fingerprint tells them apart.
            self.assertEqual(git_commit(repo), label)


class LeadingListItemIdTest(unittest.TestCase):
    def test_block_ial_after_item_does_not_replace_leading_id(self) -> None:
        # kramdown-rfc anchors the item with its leading IAL; the block IAL on the next
        # line goes on the enclosing list.
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / "draft-tempo-charge-00.md"
            draft.write_text(
                "# Rules\n\n- {: #req-tempo-charge-001} Clients MUST pay.\n"
                "{: #req-tempo-charge-002}\n",
                encoding="utf-8",
            )
            requirements, warnings, _rows = extract_requirements(draft, draft.name)
        self.assertEqual([r.req_id for r in requirements], ["req-tempo-charge-001"])
        self.assertTrue(any("req-tempo-charge-002 follows a list item" in w for w in warnings))


class AttributeListDefinitionTest(unittest.TestCase):
    def test_ial_naming_a_definition_gets_its_id(self) -> None:
        # kramdown applies an ALD's attributes to every IAL that names it, resolving the
        # names after parsing, so a definition later in the draft applies too; referenced
        # IDs go first and the IAL's own ID wins (checked with kramdown-rfc). Definitions
        # in code or comments define nothing, and a definition line tags nothing itself.
        # A reference cycle (which crashes kramdown) only has to stop rather than recurse.
        requirements, warnings, _ = read_draft(
            "Before MUST be.\n{: early}\n\n"
            "Own MUST win.\n{: early #req-tempo-charge-009}\n\n"
            "Nested MUST be.\n{: outer}\n\n"
            "Coded MUST not.\n{: coded}\n\n"
            "~~~\n{:coded: #req-tempo-charge-077}\n~~~\n\n"
            "<!-- {:hidden: #req-tempo-charge-088} -->\n\n"
            "Hidden MUST not.\n{: hidden}\n\n"
            "Loop MUST stop.\n{: loop}\n\n"
            "- {: late} Item MUST be.\n\n"
            "Defined MUST stay untagged.\n{:early: #req-tempo-charge-001}\n"
            "{:outer: inner .x}\n{:inner: #req-tempo-charge-002}\n"
            "{:late: #req-tempo-charge-003}\n{:loop: loop #req-tempo-charge-004}\n"
        )
        self.assertEqual(
            [r.req_id for r in requirements],
            [
                "req-tempo-charge-001",
                "req-tempo-charge-009",
                "req-tempo-charge-002",
                None,
                None,
                "req-tempo-charge-004",
                "req-tempo-charge-003",
                None,
            ],
        )
        self.assertEqual(warnings, [])

    def test_duplicate_check_sees_expanded_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs = write_specs(
                Path(directory),
                {
                    "specs/draft-tempo-charge-00.md": "# A\n\nX MUST hold.\n{: reqdef}\n\n"
                    "Y MUST hold.\n{: #req-tempo-charge-001}\n\n{:reqdef: #req-tempo-charge-001}\n",
                },
            )
            requirements, warnings, _, _ = extract_all(specs)
        self.assertEqual([r.id_problem for r in requirements], ["duplicate", "duplicate"])
        self.assertTrue(any("req-tempo-charge-001 already used" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
