from __future__ import annotations

import unittest

from report_abnf_fuzz import Finding, decode_keys, encode_keys, findings, issue_body, plan

RUN = "https://github.com/tempoxyz/mpp-tools/actions/runs/1"


def grammar_row(adapter: str = "go", kind: str = "accepts-invalid", mutation: str = "method-uppercase", **extra: object) -> dict:
    return {"source": "grammar", "adapter": adapter, "operation": "challenge.parse", "kind": kind, "fields": [],
            "mutations": {mutation: 3}, "count": 3, **extra}


def report(*rows: dict, reference: list[dict] | None = None, build_errors: dict | None = None) -> dict:
    return {"seed": 0, "casesPerKind": 400, "specs": "tempoxyz/mpp-specs@abc", "buildErrors": build_errors or {},
            "grammarDisagreements": list(rows), "referenceDivergences": reference or []}


class ReportAbnfFuzzTests(unittest.TestCase):
    def test_keys_group_like_the_fuzzer(self) -> None:
        rows = [
            grammar_row(),
            grammar_row(kind="value", mutation="valid", fields=["realm"]),
            grammar_row(kind="value", mutation="token-values", fields=["realm"]),
        ]
        reference = [
            {"adapter": "python", "operation": "receipt.parse", "kind": "rejects", "fields": [], "mutations": {"valid": 1}},
            {"adapter": "go", "operation": "receipt.parse", "kind": "rejects", "fields": [], "allowlisted": "known"},
        ]
        keys = [finding.key for finding in findings(report(*rows, reference=reference))]
        self.assertEqual(keys, [
            "grammar: go challenge.parse accepts-invalid [method-uppercase]",
            "grammar: go challenge.parse value (realm)",
            "mppx: python receipt.parse rejects",
        ])

    def test_report_text_is_sanitized(self) -> None:
        (finding,) = findings(report(grammar_row(adapter="go<!-- @someone -->")))
        self.assertNotIn("<", finding.key)
        self.assertNotIn("@", finding.key)

    def test_marker_round_trips(self) -> None:
        current = findings(report(grammar_row(), grammar_row(adapter="rust")))
        body = issue_body(report(), current, RUN)
        self.assertEqual(decode_keys(body), {finding.key for finding in current})
        self.assertIsNone(decode_keys("no marker"))
        self.assertEqual(decode_keys(f"<!-- abnf-fuzz-findings:{encode_keys([])} -->"), set())

    def test_opens_an_issue_and_fails_on_first_findings(self) -> None:
        action = plan(report(grammar_row()), "1", None, RUN)
        assert action.create is not None
        self.assertIn("| go | accepts-invalid | 1 |", action.create)
        self.assertTrue(action.failed)

    def test_same_findings_refresh_the_body_without_a_comment(self) -> None:
        previous = issue_body(report(), findings(report(grammar_row())), RUN)
        action = plan(report(grammar_row()), "1", previous, RUN)
        self.assertIsNotNone(action.edit)
        self.assertIsNone(action.comment)
        self.assertFalse(action.failed)

    def test_comments_on_new_and_resolved_findings(self) -> None:
        previous = issue_body(report(), findings(report(grammar_row(), grammar_row(adapter="rust"))), RUN)
        action = plan(report(grammar_row(), grammar_row(adapter="python")), "1", previous, RUN)
        assert action.comment is not None
        self.assertIn("1 new and 1 resolved findings, 2 in all.", action.comment)
        self.assertIn("- grammar: python challenge.parse accepts-invalid [method-uppercase]", action.comment)
        self.assertIn("- grammar: rust challenge.parse accepts-invalid [method-uppercase]", action.comment)
        self.assertTrue(action.failed)
        # Only resolved findings: comment, but the run passes.
        action = plan(report(grammar_row()), "1", previous, RUN)
        self.assertIsNotNone(action.comment)
        self.assertFalse(action.failed)

    def test_closes_once_adapters_agree(self) -> None:
        action = plan(report(), "0", "body", RUN)
        self.assertIsNotNone(action.close)
        self.assertFalse(action.failed)
        self.assertEqual(plan(report(), "0", None, RUN), type(action)())

    def test_build_errors_and_missing_reports_fail_without_closing(self) -> None:
        action = plan(report(build_errors={"go": "did not start"}), "1", "body", RUN)
        self.assertIsNone(action.close)
        self.assertTrue(action.failed)
        action = plan(None, "1", "body", RUN)
        self.assertIsNotNone(action.comment)
        self.assertIsNone(action.edit)
        self.assertTrue(action.failed)
        self.assertTrue(plan(report(), "2", None, RUN).failed)

    def test_finding_is_hashable(self) -> None:
        self.assertEqual(len({Finding("grammar", "go", "op", "crash", (), ()), Finding("grammar", "go", "op", "crash", (), ())}), 1)


if __name__ == "__main__":
    unittest.main()
