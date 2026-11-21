from __future__ import annotations

import json
import random
import unittest
from collections import Counter

from abnf_fuzz import (
    ACCEPT_PAYMENT_MUTATIONS,
    CHALLENGE_MUTATIONS,
    CREDENTIAL_MUTATIONS,
    KINDS,
    METHOD_MUTATIONS,
    RECEIPT_MUTATIONS,
    Case,
    Comparison,
    Generator,
    Grammar,
    Verdict,
    accept_payment_verdict,
    b64url,
    challenge_verdict,
    credential_verdict,
    expand_lists,
    extract_abnf_blocks,
    generate_cases,
    grammar_disagreement,
    parse_rules,
    receipt_verdict,
)
from diff_fuzz import parse_allowlist
from harness import CONFORMANCE_DIR

# The "ABNF Collected" appendix of specs/core/draft-httpauth-payment-01.md.
DRAFT = '''
# ABNF Collected

~~~abnf
; HTTP Authentication Challenge (following RFC 7235 Section 2.1)
payment-challenge = "Payment" [ 1*SP auth-params ]
auth-params       = auth-param *( OWS "," OWS auth-param )
auth-param        = token BWS "=" BWS ( token / quoted-string )

payment-credentials = "Payment" 1*SP base64url-nopad
Payment-Authorization = payment-credentials

; Client payment preferences
Accept-Payment = #payment-range
payment-range = payment-token [ weight ]
payment-token = payment-method-or-wildcard "/" intent-or-wildcard
payment-method-or-wildcard = payment-method-id / "*"
intent-or-wildcard = intent-token / "*"

Payment-Receipt = base64url-nopad
base64url-nopad = 1*( ALPHA / DIGIT / "-" / "_" )

; Payment method identifier (lowercase only)
payment-method-id   = 1*LOWERALPHA
LOWERALPHA          = %x61-7A  ; a-z

intent-token = 1*( ALPHA / DIGIT / "-" )
~~~

~~~
not = "abnf"
~~~
'''

GRAMMAR = Grammar.from_markdown(DRAFT)
REQUEST = b64url('{"amount":"1"}')


def challenge(**params: str) -> str:
    values = {"id": '"ch_1"', "realm": '"api.example.com"', "method": '"tempo"', "intent": '"charge"', "request": f'"{REQUEST}"'}
    values.update(params)
    return "Payment " + ", ".join(f"{key}={value}" for key, value in values.items())


def credential_token(method: str = "tempo", payload: object = None) -> str:
    document = {
        "challenge": {"id": "ch_1", "realm": "r", "method": method, "intent": "charge", "request": REQUEST},
        "payload": {} if payload is None else payload,
    }
    return b64url(json.dumps(document))


def receipt_token(**overrides: object) -> str:
    document = {"status": "success", "method": "tempo", "timestamp": "2026-01-29T12:00:30Z", "reference": "0xab"}
    document.update(overrides)
    return b64url(json.dumps(document))


class ExtractionTests(unittest.TestCase):
    def test_extracts_only_abnf_blocks(self) -> None:
        blocks = extract_abnf_blocks(DRAFT + "\n```abnf\nx = \"y\"\n```\n")
        self.assertEqual(len(blocks), 2)
        self.assertIn("payment-challenge", blocks[0])
        self.assertEqual(blocks[1].strip(), 'x = "y"')

    def test_joins_continuations_and_strips_comments(self) -> None:
        rules = parse_rules(['a = "x" ; comment\n    / "y"\nb = ";" ; a quoted semicolon stays\n'])
        self.assertEqual(rules["a"], ("a", '"x" / "y"'))
        self.assertEqual(rules["b"], ("b", '";"'))

    def test_repeated_identical_rules_are_kept_once_and_conflicts_fail(self) -> None:
        self.assertEqual(len(parse_rules(['a = "x"', 'a  =  "x"'])), 1)
        with self.assertRaises(ValueError):
            parse_rules(['a = "x"', 'a = "y"'])
        with self.assertRaises(ValueError):
            parse_rules(['a =/ "x"'])

    def test_expands_rfc9110_list_rules(self) -> None:
        self.assertEqual(expand_lists("#x"), '[ x *( OWS "," OWS x ) ]')
        self.assertEqual(expand_lists("1#x"), '( x *( OWS "," OWS x ) )')
        self.assertEqual(expand_lists('"#x"'), '"#x"')
        with self.assertRaises(ValueError):
            expand_lists("2#3x")

    def test_core_and_rfc9110_rules_resolve(self) -> None:
        self.assertTrue(GRAMMAR.accepts("payment-method-id", "tempo"))
        for method in ("Tempo", "tempo2", "tempo:testnet", "tempo-v2", "tempo_v2", ""):
            self.assertFalse(GRAMMAR.accepts("payment-method-id", method), method)
        self.assertTrue(GRAMMAR.accepts("auth-param", 'realm = "a\\"b"'))
        self.assertFalse(GRAMMAR.accepts("auth-param", 'realm="a"b"'))


class VerdictTests(unittest.TestCase):
    def test_challenge_values_follow_quoted_pair(self) -> None:
        verdict = challenge_verdict(GRAMMAR, challenge(realm='"merchant\\u2014api.example.com"', method="tempo"))
        self.assertTrue(verdict.accept)
        assert verdict.expected is not None
        self.assertEqual(verdict.expected["realm"], "merchantu2014api.example.com")
        self.assertEqual(verdict.expected["method"], "tempo")
        self.assertEqual(verdict.expected["request"], {"amount": "1"})

    def test_challenge_near_misses_are_rejected(self) -> None:
        rejected = {
            "uppercase method": challenge(method='"Tempo"'),
            "digit in method": challenge(method='"tempo2"'),
            "colon in method": challenge(method='"tempo:x"'),
            "unescaped quote": challenge(realm='"a"b"'),
            "control character": challenge(realm='"a\x00b"'),
            "padded request": challenge(request=f'"{REQUEST}=="'),
            "duplicate parameter": challenge() + ', Realm="other"',
            "trailing comma": challenge() + ",",
            "tab after scheme": challenge().replace("Payment ", "Payment\t"),
            "empty id": challenge(id='""'),
        }
        for label, header in rejected.items():
            self.assertFalse(challenge_verdict(GRAMMAR, header).accept, label)

    def test_challenge_whitespace_and_tokens_are_accepted(self) -> None:
        header = challenge(method="tempo", id="ch_1").replace("realm=", "realm \t= ").replace(", intent", "\t,intent")
        self.assertTrue(challenge_verdict(GRAMMAR, header).accept)
        self.assertTrue(challenge_verdict(GRAMMAR, challenge().replace("Payment", "pAYMENT")).accept)

    def test_credentials(self) -> None:
        self.assertTrue(credential_verdict(GRAMMAR, "Payment " + credential_token()).accept)
        for header in (
            "Payment " + credential_token("Tempo"),
            "Payment " + credential_token() + "=",
            "Payment\t" + credential_token(),
            "Payment " + credential_token() + ", Basic x",
            "Payment " + b64url("[1]"),
            "Payment " + credential_token(payload=[]),
        ):
            self.assertFalse(credential_verdict(GRAMMAR, header).accept, header)

    def test_receipts(self) -> None:
        self.assertTrue(receipt_verdict(GRAMMAR, receipt_token()).accept)
        for header in (receipt_token(method="tempo2"), receipt_token(timestamp="yesterday"), receipt_token() + "=", "e30 e30"):
            self.assertFalse(receipt_verdict(GRAMMAR, header).accept, header)

    def test_accept_payment_examples_from_the_draft(self) -> None:
        for header in (
            "tempo/charge, tempo/session, stripe/charge;q=0.5, solana/charge;q=0.3",
            "tempo/*, solana/*;q=0.6, */session;q=0.3",
            "tempo/charge, tempo/session;q=0, solana/charge",
        ):
            self.assertTrue(accept_payment_verdict(GRAMMAR, header).accept, header)
        for header in ("Tempo/charge", "tempo2/charge", "tempo", "tempo/charge;q=2", "tempo/charge,"):
            self.assertFalse(accept_payment_verdict(GRAMMAR, header).accept, header)

    def test_agrees_with_the_parse_vectors(self) -> None:
        checks = {
            "www-authenticate.json": challenge_verdict,
            "authorization.json": credential_verdict,
            "receipt.json": receipt_verdict,
        }
        for name, verdict in checks.items():
            document = json.loads((CONFORMANCE_DIR / "vectors" / name).read_text(encoding="utf-8"))
            for scenario in document["scenarios"]:
                parse = scenario.get("tests", {}).get("parse")
                # Vectors tagged permissive-parsing pin what SDKs accept beyond the grammar.
                if not isinstance(scenario.get("wire"), str) or parse is None or "permissive-parsing" in scenario.get("tags", []):
                    continue
                expected = parse is True or (isinstance(parse, dict) and parse.get("success", True))
                with self.subTest(vector=f"{name}:{scenario['name']}"):
                    self.assertEqual(verdict(GRAMMAR, scenario["wire"]).accept, expected)


class GenerationTests(unittest.TestCase):
    def test_generated_strings_match_their_rule(self) -> None:
        generator = Generator(GRAMMAR, random.Random(7))
        for rule in ("payment-method-id", "intent-token", "token", "quoted-string", "auth-params", "Accept-Payment", "payment-challenge"):
            for _ in range(25):
                value = generator.generate(rule, max_length=400)
                self.assertTrue(GRAMMAR.accepts(rule, value), f"{rule}: {value!r}")
                self.assertTrue(value.isascii())

    def test_cases_are_deterministic_for_a_seed(self) -> None:
        first = generate_cases(GRAMMAR, seed=3, cases=60, kinds=list(KINDS))
        again = generate_cases(GRAMMAR, seed=3, cases=60, kinds=list(KINDS))
        other = generate_cases(GRAMMAR, seed=4, cases=60, kinds=list(KINDS))
        self.assertEqual([case.input for case in first], [case.input for case in again])
        self.assertNotEqual([case.input for case in first], [case.input for case in other])

    def test_every_mutation_is_generated_with_the_expected_verdict(self) -> None:
        cases = generate_cases(GRAMMAR, seed=0, cases=200, kinds=list(KINDS))
        seen = Counter((case.operation or case.kind, case.mutation) for case in cases)
        for operation, mutations in (
            ("challenge.parse", CHALLENGE_MUTATIONS),
            ("challenge.format", METHOD_MUTATIONS),
            ("credential.parse", CREDENTIAL_MUTATIONS),
            ("receipt.parse", RECEIPT_MUTATIONS),
            ("accept-payment", ACCEPT_PAYMENT_MUTATIONS),
        ):
            for mutation in ("valid", *mutations):
                self.assertGreater(seen[(operation, mutation)], 0, f"{operation} {mutation}")
        accepted = {"valid", "scheme-case-and-spaces", "token-values", "mixed-whitespace", "quoted-pair-unicode-escape",
                    "quoted-pair-any-char", "bws-around-equals", "ows-around-comma"}
        for case in cases:
            with self.subTest(case=case.input, mutation=case.mutation):
                self.assertEqual(case.verdict.accept, case.mutation in accepted, case.verdict.reason)


def ok(value: object) -> dict:
    return {"ok": True, "value": value}


def parse_error() -> dict:
    return {"ok": False, "error": {"type": "parse_error", "message": "invalid method"}}


class ComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        header = challenge(realm='"merchant\\u2014api"')
        self.valid = Case("challenge", "challenge.parse", "quoted-pair-unicode-escape", {"header": header}, challenge_verdict(GRAMMAR, header))
        bad = challenge(method='"Tempo"')
        self.invalid = Case("challenge", "challenge.parse", "method-uppercase", {"header": bad}, challenge_verdict(GRAMMAR, bad))
        self.parsed = {"id": "ch_1", "realm": "merchantu2014api", "method": "tempo", "intent": "charge", "request": REQUEST}

    def test_classifies_grammar_disagreements(self) -> None:
        self.assertIsNone(grammar_disagreement(GRAMMAR, self.valid, ok(self.parsed)))
        self.assertEqual(grammar_disagreement(GRAMMAR, self.valid, parse_error()), ("rejects-valid", []))
        self.assertEqual(
            grammar_disagreement(GRAMMAR, self.valid, ok({**self.parsed, "realm": "merchant\u2014api"})), ("value", ["realm"]),
        )
        self.assertEqual(grammar_disagreement(GRAMMAR, self.invalid, ok(self.parsed)), ("accepts-invalid", []))
        self.assertIsNone(grammar_disagreement(GRAMMAR, self.invalid, parse_error()))
        crash = {"ok": False, "error": {"type": "unknown_error", "message": "boom"}}
        self.assertEqual(grammar_disagreement(GRAMMAR, self.invalid, crash), ("crash", []))

    def test_checks_formatted_headers_against_the_grammar(self) -> None:
        fields = {"id": "ch_1", "realm": "r", "method": "tempo", "intent": "charge", "request": {"amount": "1"}}
        case = Case("challenge", "challenge.format", "valid", fields, Verdict(True, "accepted", fields))
        good = 'Payment id="ch_1", realm="r", method="tempo", intent="charge", request="' + REQUEST + '"'
        self.assertIsNone(grammar_disagreement(GRAMMAR, case, ok({"header": good})))
        self.assertEqual(grammar_disagreement(GRAMMAR, case, ok({"header": good + ","})), ("invalid-output", ["payment-challenge"]))
        self.assertEqual(
            grammar_disagreement(GRAMMAR, case, ok({"header": good.replace('realm="r"', 'realm="s"')})), ("value", ["realm"]),
        )

    def test_checks_formatted_credentials_with_the_full_verdict(self) -> None:
        echo = {"id": "ch_1", "realm": "r", "method": "tempo", "intent": "charge", "request": {"amount": "1"}}
        expected = {"challenge": echo, "payload": {"signature": "0xab"}}
        case = Case("credential", "credential.format", "valid", expected, Verdict(True, "accepted", expected))

        def header(**challenge: object) -> str:
            document = {"challenge": {**echo, "request": REQUEST, **challenge}, "payload": {"signature": "0xab"}}
            return "Payment " + b64url(json.dumps(document))

        self.assertIsNone(grammar_disagreement(GRAMMAR, case, ok({"header": header()})))
        # Matches payment-credentials, but is not a base64url JSON document.
        self.assertEqual(
            grammar_disagreement(GRAMMAR, case, ok({"header": "Payment a"})),
            ("invalid-output", ["credential is not a base64url-nopad JSON object"]),
        )
        self.assertEqual(
            grammar_disagreement(GRAMMAR, case, ok({"header": header(method="Tempo")})),
            ("invalid-output", ["challenge.method does not match payment-method-id"]),
        )
        self.assertEqual(grammar_disagreement(GRAMMAR, case, ok({"header": header(realm="s")})), ("value", ["challenge.realm"]))
        other_request = header(request=b64url('{"amount":"2"}'))
        self.assertEqual(grammar_disagreement(GRAMMAR, case, ok({"header": other_request})), ("value", ["challenge.request"]))

    def test_checks_formatted_receipts_with_the_full_verdict(self) -> None:
        document = {"status": "success", "method": "tempo", "timestamp": "2026-01-29T12:00:30Z", "reference": "0xab"}
        expected = {"status": "success", "method": "tempo", "reference": "0xab"}
        case = Case("receipt", "receipt.format", "valid", document, Verdict(True, "accepted", expected))
        self.assertIsNone(grammar_disagreement(GRAMMAR, case, ok({"header": receipt_token()})))
        self.assertEqual(
            grammar_disagreement(GRAMMAR, case, ok({"header": "a"})),
            ("invalid-output", ["receipt is not a base64url-nopad JSON object"]),
        )
        self.assertEqual(
            grammar_disagreement(GRAMMAR, case, ok({"header": receipt_token(timestamp="yesterday")})),
            ("invalid-output", ["receipt timestamp is not an RFC 3339 date-time"]),
        )
        self.assertEqual(grammar_disagreement(GRAMMAR, case, ok({"header": receipt_token(reference="0xcd")})), ("value", ["reference"]))

    def test_format_cases_carry_the_values_to_compare(self) -> None:
        cases = generate_cases(GRAMMAR, seed=0, cases=40, kinds=["credential", "receipt"])
        for case in cases:
            if case.operation in ("credential.format", "receipt.format"):
                self.assertIsNotNone(case.verdict.expected, case.operation)

    def test_reports_grammar_and_reference_disagreements(self) -> None:
        allowlist = parse_allowlist({"entries": [{
            "id": "known", "adapters": ["go"], "operations": ["challenge.parse"], "kinds": ["rejects"],
            "category": "harmless", "reason": "known",
        }]})
        comparison = Comparison(GRAMMAR, allowlist)
        comparison.add(self.invalid, {"typescript": ok(self.parsed), "python": parse_error(), "go": parse_error()})
        comparison.add(self.valid, {"typescript": ok(self.parsed), "python": ok(self.parsed), "go": ok(self.parsed)})
        comparison.add(self.valid, {"typescript": ok(self.parsed), "python": ok({**self.parsed, "realm": "x"}), "go": ok(self.parsed)})
        grammar = {(f.adapter, f.kind) for f in comparison.grammar_findings.values()}
        self.assertEqual(grammar, {("typescript", "accepts-invalid"), ("python", "value")})
        new = {(f.adapter, f.kind) for f in comparison.new_reference_findings}
        self.assertEqual(new, {("python", "rejects"), ("python", "value")})
        self.assertEqual(comparison.disagreements, 4)
        summary = comparison.summary(seed=1, cases=3, adapters=["go", "python", "typescript"], specs="specs", build_errors={})
        self.assertIn("| typescript | challenge.parse | accepts-invalid | method-uppercase | 1 |", summary)
        self.assertIn("Allowlisted: known 1.", summary)
        report = comparison.report(seed=1, cases=3, adapters=[], specs="specs", build_errors={})
        self.assertEqual(len(report["grammarDisagreements"]), 2)


if __name__ == "__main__":
    unittest.main()
