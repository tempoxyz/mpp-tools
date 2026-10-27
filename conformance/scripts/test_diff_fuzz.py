from __future__ import annotations

import json
import unittest

from harness import validate_value, validate_vector_scenarios
from diff_fuzz import (
    ALLOWLIST_PATH,
    Divergence,
    VECTOR_FILES,
    classify,
    diff_fields,
    find_allowlisted,
    load_allowlist,
    normalize_value,
    outcome,
    parse_allowlist,
    vector_scenario,
)


def entry(**overrides):
    raw = {
        "id": "python-scheme-space",
        "adapters": ["python"],
        "operations": ["challenge.parse"],
        "kinds": ["rejects"],
        "category": "harmless",
        "reason": "pympp accepts only SP after Payment",
    }
    raw.update(overrides)
    return raw


def divergence(**overrides):
    values = {
        "operation": "challenge.parse",
        "adapter": "python",
        "kind": "rejects",
        "fields": (),
        "input": {"header": 'Payment\tid="a"'},
        "reference": {"ok": True, "value": {"id": "a"}},
        "actual": {"ok": False, "error": {"type": "parse_error", "message": "bad"}},
    }
    values.update(overrides)
    return Divergence(**values)


class NormalizeTests(unittest.TestCase):
    def test_null_optional_fields_count_as_absent(self) -> None:
        value = {"id": "a", "realm": "r", "method": "m", "intent": "i", "request": {}, "expires": None}
        self.assertEqual(
            normalize_value("challenge.parse", value),
            {"id": "a", "realm": "r", "method": "m", "intent": "i", "request": {}},
        )

    def test_empty_optional_fields_count_as_absent_but_required_ones_stay(self) -> None:
        value = {"id": "a", "realm": "", "method": "m", "intent": "i", "request": {}, "description": "", "opaque": ""}
        self.assertEqual(
            normalize_value("challenge.parse", value),
            {"id": "a", "realm": "", "method": "m", "intent": "i", "request": {}},
        )
        receipt = {"status": "success", "timestamp": "t", "reference": "r", "externalId": "", "extra": {}}
        self.assertEqual(normalize_value("receipt.parse", receipt), {"status": "success", "timestamp": "t", "reference": "r"})

    def test_large_integers_compare_as_doubles(self) -> None:
        reference = {"status": "success", "timestamp": "t", "reference": "r", "extra": {"amount": 55411802852030330}}
        actual = {"status": "success", "timestamp": "t", "reference": "r", "extra": {"amount": 5.541180285203033e16}}
        self.assertEqual(normalize_value("receipt.parse", reference), normalize_value("receipt.parse", actual))

    def test_nulls_inside_request_are_kept(self) -> None:
        value = {"id": "a", "request": {"memo": None}}
        self.assertEqual(normalize_value("challenge.parse", value)["request"], {"memo": None})

    def test_credential_challenge_request_compares_decoded(self) -> None:
        encoded = {"challenge": {"id": "a", "request": "eyJhIjoxfQ", "digest": None}, "payload": {}, "source": None}
        decoded = {"challenge": {"id": "a", "request": {"a": 1}}, "payload": {}}
        self.assertEqual(
            normalize_value("credential.parse", encoded),
            normalize_value("credential.parse", decoded),
        )

    def test_outcome_classes(self) -> None:
        self.assertEqual(outcome("receipt.parse", {"ok": True, "value": {"status": "success", "method": None}}),
                         {"class": "ok", "value": {"status": "success"}})
        self.assertEqual(outcome("receipt.parse", {"ok": False, "error": {"type": "parse_error", "message": "x"}}),
                         {"class": "error", "type": "parse_error"})
        self.assertEqual(outcome("receipt.parse", {"ok": False, "error": {"type": "unknown_error", "message": "Timeout"}})["class"],
                         "crash")


class ErrorCauseTests(unittest.TestCase):
    def test_input_values_do_not_split_causes(self) -> None:
        from diff_fuzz import error_cause
        self.assertEqual(
            error_cause("Invalid payment method id: 'a0'"),
            error_cause("Invalid payment method id: 'zz9'"),
        )
        self.assertEqual(error_cause("illegal base64 data at input byte 12"), "illegal base64 data at input byte _")

    def test_mppx_issue_lists_reduce_to_paths_and_codes(self) -> None:
        from diff_fuzz import error_cause
        message = json.dumps([{"code": "invalid_type", "path": ["method"], "message": "Invalid input"}], indent=2)
        self.assertEqual(error_cause(message), "method:invalid_type")


class ClassifyTests(unittest.TestCase):
    ok = {"class": "ok", "value": {"id": "a", "request": {"x": 1}}}
    error = {"class": "error", "type": "parse_error"}
    crash = {"class": "crash", "message": "boom"}

    def test_matching_outcomes_do_not_diverge(self) -> None:
        self.assertIsNone(classify(self.ok, dict(self.ok)))
        self.assertIsNone(classify(self.error, {"class": "error", "type": "parse_error"}))
        self.assertIsNone(classify(self.crash, self.crash))

    def test_success_class_mismatches(self) -> None:
        self.assertEqual(classify(self.error, self.ok), ("accepts", ()))
        self.assertEqual(classify(self.ok, self.error), ("rejects", ()))
        self.assertEqual(classify(self.ok, self.crash), ("crash", ()))
        self.assertEqual(classify(self.crash, self.ok), ("reference_crash", ()))

    def test_value_mismatch_names_fields(self) -> None:
        actual = {"class": "ok", "value": {"id": "b", "request": {"x": 2}, "extra": 1}}
        self.assertEqual(classify(self.ok, actual), ("value", ("extra", "id", "request")))

    def test_diff_fields_descends_into_challenge_but_not_opaque_values(self) -> None:
        expected = {"challenge": {"id": "a", "request": {"a": 1, "b": 2}}, "payload": {"x": 1}}
        actual = {"challenge": {"id": "b", "request": {"a": 1, "b": 3}}, "payload": {"x": 2}}
        self.assertEqual(diff_fields(expected, actual), ("challenge.id", "challenge.request", "payload"))


class AllowlistTests(unittest.TestCase):
    def test_checked_in_allowlist_is_valid(self) -> None:
        for item in load_allowlist(ALLOWLIST_PATH):
            self.assertTrue(item.reason)

    def test_matches_adapter_operation_and_kind(self) -> None:
        entries = parse_allowlist({"entries": [entry()]})
        self.assertIsNotNone(find_allowlisted(entries, divergence()))
        self.assertIsNone(find_allowlisted(entries, divergence(adapter="go")))
        self.assertIsNone(find_allowlisted(entries, divergence(kind="accepts")))
        self.assertIsNone(find_allowlisted(entries, divergence(operation="credential.parse")))

    def test_wildcard_adapter(self) -> None:
        entries = parse_allowlist({"entries": [entry(adapters=["*"])]})
        self.assertIsNotNone(find_allowlisted(entries, divergence(adapter="go")))

    def test_input_pattern_matches_header(self) -> None:
        entries = parse_allowlist({"entries": [entry(inputPattern=r"^Payment\t")]})
        self.assertIsNotNone(find_allowlisted(entries, divergence()))
        self.assertIsNone(find_allowlisted(entries, divergence(input={"header": 'Payment  id="a"'})))

    def test_fields_must_cover_every_differing_field(self) -> None:
        entries = parse_allowlist({"entries": [entry(kinds=["value"], fields=["timestamp"])]})
        self.assertIsNotNone(find_allowlisted(entries, divergence(kind="value", fields=("timestamp",))))
        self.assertIsNone(find_allowlisted(entries, divergence(kind="value", fields=("timestamp", "method"))))

    def test_value_fields_compose_across_entries(self) -> None:
        entries = parse_allowlist({"entries": [
            entry(id="a", kinds=["value"], fields=["timestamp"]),
            entry(id="b", kinds=["value"], fields=["extra"]),
        ]})
        self.assertIsNotNone(find_allowlisted(entries, divergence(kind="value", fields=("extra", "timestamp"))))
        self.assertIsNone(find_allowlisted(entries, divergence(kind="value", fields=("extra", "method"))))

    def test_message_pattern_matches_failing_side(self) -> None:
        entries = parse_allowlist({"entries": [entry(messagePattern="Invalid payment method")]})
        rejected = divergence(actual={"ok": False, "error": {"type": "parse_error", "message": "Invalid payment method id: 'a0'"}})
        self.assertIsNotNone(find_allowlisted(entries, rejected))
        self.assertIsNone(find_allowlisted(entries, divergence()))

    def test_rejects_invalid_entries(self) -> None:
        cases = [
            ({"entries": [entry(reason="")]}, "reason"),
            ({"entries": [entry(kinds=["different"])]}, "unknown kinds"),
            ({"entries": [entry(operations=["http.payment_request"])]}, "unknown operations"),
            ({"entries": [entry(category="maybe")]}, "category"),
            ({"entries": [entry(inputPattern="(")]}, "inputPattern"),
            ({"entries": [entry(), entry()]}, "duplicates id"),
            ({"entries": [entry(extra=True)]}, "unknown keys"),
            ({"entries": [entry(adapters=[])]}, "adapters"),
            ({}, "entries list"),
        ]
        for document, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                parse_allowlist(document)


class VectorScenarioTests(unittest.TestCase):
    def assert_valid_vector(self, operation: str, scenario: dict) -> None:
        commands = {
            "challenge.parse": {"parse": "parse-www-authenticate", "format": "format-www-authenticate"},
            "challenge.format": {"parse": "parse-www-authenticate", "format": "format-www-authenticate"},
            "receipt.parse": {"parse": "parse-receipt", "format": "format-receipt"},
            "base64url.decode": {"parse": "base64url-decode", "format": "base64url-encode"},
            "challenge.id": {"generate": "generate-challenge-id"},
        }[operation]
        vector = {
            "version": "1.0.0",
            "description": "fuzz",
            "spec_ref": "fuzz",
            "commands": commands,
            "scenarios": [scenario],
        }
        validate_value(vector, "vector.schema.json", "fuzz vector")
        validate_vector_scenarios(vector, "fuzz vector")

    def test_parse_success_reproducer(self) -> None:
        value = {"id": "a", "realm": "r", "method": "m", "intent": "i", "request": {}}
        scenario = vector_scenario(divergence(reference={"ok": True, "value": value}))
        self.assertEqual(scenario["wire"], 'Payment\tid="a"')
        self.assertEqual(scenario["object"], value)
        self.assertEqual(scenario["tests"], {"parse": True})
        self.assert_valid_vector("challenge.parse", scenario)

    def test_parse_error_reproducer(self) -> None:
        scenario = vector_scenario(divergence(
            operation="receipt.parse",
            kind="accepts",
            input={"header": "=e30"},
            reference={"ok": False, "error": {"type": "parse_error", "message": "x"}},
            actual={"ok": True, "value": {}},
        ))
        self.assertNotIn("object", scenario)
        self.assertEqual(scenario["tests"], {"parse": {"success": False, "error_type": "parse_error"}})
        self.assert_valid_vector("receipt.parse", scenario)

    def test_format_and_text_and_generate_reproducers(self) -> None:
        challenge = {"id": "a", "realm": "r", "method": "m", "intent": "i", "request": {}}
        scenario = vector_scenario(divergence(
            operation="challenge.format", kind="value", input=challenge,
            reference={"ok": True, "value": {"header": 'Payment id="a"'}},
        ))
        self.assertEqual(scenario["object"], challenge)
        self.assertEqual(scenario["wire"], 'Payment id="a"')
        self.assert_valid_vector("challenge.format", scenario)

        scenario = vector_scenario(divergence(
            operation="base64url.decode", input={"text": "YQ"},
            reference={"ok": True, "value": {"text": "a"}},
        ))
        self.assertEqual((scenario["encoded"], scenario["decoded"]), ("YQ", "a"))
        self.assert_valid_vector("base64url.decode", scenario)

        params = {"secretKey": "k", "realm": "r", "method": "m", "intent": "i", "request": {}}
        scenario = vector_scenario(divergence(
            operation="challenge.id", input=params,
            reference={"ok": True, "value": {"id": "abc"}},
        ))
        self.assertEqual((scenario["input"], scenario["expected"]), (params, "abc"))
        self.assert_valid_vector("challenge.id", scenario)

    def test_every_operation_names_a_vector_file(self) -> None:
        from harness import VECTORS_DIR
        for vector_file in VECTOR_FILES.values():
            self.assertTrue((VECTORS_DIR / vector_file).exists(), vector_file)

    def test_reproducer_is_json_serializable(self) -> None:
        json.dumps(vector_scenario(divergence()))


if __name__ == "__main__":
    unittest.main()

