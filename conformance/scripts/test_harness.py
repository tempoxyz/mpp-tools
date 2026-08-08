from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from harness import (
    AdapterClient,
    AdapterConfig,
    FLOW_CASES_PATH,
    FLOW_RESULTS_PATH,
    SCHEMA_STORE,
    VECTORS_DIR,
    load_flow_cases,
    load_flow_results,
    load_json,
    load_vector,
    validate_value,
    validate_vector_scenarios,
    write_flow_results,
)


class AdapterClientValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AdapterConfig(
            name="test",
            command=["test-adapter"],
            capabilities=["challenge.format"],
        )
        self.client = AdapterClient(self.adapter)
        self.invalid_challenge = {
            "id": "",
            "realm": "api.example.com",
            "method": "tempo",
            "intent": "charge",
            "request": {},
        }

    @patch("harness.subprocess.run")
    def test_validates_operation_input_by_default(self, run) -> None:
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            self.client.call("challenge.format", self.invalid_challenge)

        run.assert_not_called()

    @patch("harness.subprocess.run")
    def test_expected_failure_can_reach_adapter(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(
            {
                "ok": False,
                "error": {"type": "format_error", "message": "id is required"},
            }
        )

        result = self.client.call(
            "challenge.format",
            self.invalid_challenge,
            validate_input=False,
        )

        self.assertEqual(result["error"]["type"], "format_error")
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["input"]["id"], "")

    @patch("harness.subprocess.run")
    def test_expected_failure_still_validates_request_envelope(self, run) -> None:
        with self.assertRaisesRegex(ValueError, "Additional properties are not allowed"):
            self.client.call(
                "challenge.format",
                self.invalid_challenge,
                context={"unexpected": True},
                validate_input=False,
            )

        run.assert_not_called()


class RepositoryDataSchemaTests(unittest.TestCase):
    def test_repository_schemas_are_valid(self) -> None:
        for name, schema in SCHEMA_STORE.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)

    def test_repository_documents_validate(self) -> None:
        vectors = [
            load_vector(path)
            for path in sorted(VECTORS_DIR.glob("*.json"))
            if path.name != "package.json"
        ]

        self.assertGreater(len(vectors), 0)
        self.assertGreater(len(load_flow_cases()), 0)
        self.assertGreater(len(load_flow_results()), 0)

    def test_vector_rejects_unknown_scenario_field(self) -> None:
        vector = load_json(VECTORS_DIR / "authorization.json")
        vector["scenarios"][0]["typo"] = True

        with self.assertRaisesRegex(ValueError, "Additional properties are not allowed"):
            validate_value(vector, "vector.schema.json", "vector")

    def test_flow_case_requires_path(self) -> None:
        document = load_json(FLOW_CASES_PATH)
        del document["cases"][0]["path"]

        with self.assertRaisesRegex(ValueError, "'path' is a required property"):
            validate_value(document, "flow-cases.schema.json", "flows")

    def test_flow_result_requires_numeric_status(self) -> None:
        document = load_json(FLOW_RESULTS_PATH)
        document["results"][0]["outcome"]["status"] = "200"

        with self.assertRaisesRegex(ValueError, "is not of type 'integer'"):
            validate_value(document, "flow-results.schema.json", "golden")

    def test_flow_case_accepts_client_http_fields(self) -> None:
        document = load_json(FLOW_CASES_PATH)
        flow_case = document["cases"][0]
        flow_case.update(
            {
                "client_flow": True,
                "headers": {"X-Test": "value"},
                "source": {"account": "test"},
                "expect_response_name": "paid",
            }
        )

        validate_value(document, "flow-cases.schema.json", "flows")

    def test_operation_vector_requires_input_and_expected(self) -> None:
        vector = load_json(VECTORS_DIR / "tempo-proof.json")
        scenario = vector["scenarios"][0]
        del scenario["input"]
        del scenario["expected"]
        scenario.update({"wire": "unused", "object": {}, "tests": {"parse": True}})

        with self.assertRaisesRegex(ValueError, "'input' is a required property"):
            validate_value(vector, "vector.schema.json", "vector")

    def test_parse_format_vector_requires_tests(self) -> None:
        vector = load_json(VECTORS_DIR / "authorization.json")
        scenario = vector["scenarios"][0]
        del scenario["tests"]
        scenario.update({"input": {}, "expected": {}})

        with self.assertRaisesRegex(ValueError, "'tests' is a required property"):
            validate_value(vector, "vector.schema.json", "vector")

    def test_successful_parse_requires_expected_object(self) -> None:
        vector = load_json(VECTORS_DIR / "authorization.json")
        del vector["scenarios"][0]["object"]

        with self.assertRaisesRegex(ValueError, "successful parse test requires object"):
            validate_vector_scenarios(vector, "vector")

    def test_roundtrip_requires_only_input_object(self) -> None:
        vector = load_json(VECTORS_DIR / "authorization.json")
        scenario = vector["scenarios"][0]
        scenario["tests"] = {"roundtrip": True}
        del scenario["wire"]

        validate_vector_scenarios(vector, "vector")

    def test_parse_format_rejects_unsupported_generate_test(self) -> None:
        vector = load_json(VECTORS_DIR / "authorization.json")
        vector["scenarios"][0]["tests"] = {"generate": True}

        with self.assertRaisesRegex(ValueError, "unsupported tests: generate"):
            validate_vector_scenarios(vector, "vector")

    def test_header_parse_requires_wire_not_encoded(self) -> None:
        vector = load_json(VECTORS_DIR / "authorization.json")
        scenario = vector["scenarios"][0]
        scenario["encoded"] = scenario.pop("wire")

        with self.assertRaisesRegex(ValueError, "parse test requires wire"):
            validate_vector_scenarios(vector, "vector")

    def test_base64url_parse_requires_encoded_not_wire(self) -> None:
        vector = load_json(VECTORS_DIR / "base64url.json")
        scenario = vector["scenarios"][0]
        scenario["wire"] = scenario.pop("encoded")

        with self.assertRaisesRegex(ValueError, "parse test requires encoded"):
            validate_vector_scenarios(vector, "vector")

    def test_client_fields_require_client_flow(self) -> None:
        document = load_json(FLOW_CASES_PATH)
        document["cases"][0]["source"] = "test-source"

        with self.assertRaisesRegex(ValueError, "'client_flow' is a required property"):
            validate_value(document, "flow-cases.schema.json", "flows")

    def test_flow_result_rejects_invalid_date_time(self) -> None:
        document = {
            "results": [
                {
                    "name": "invalid_timestamp",
                    "outcome": {"ok": True, "status": 200},
                    "receipt": {
                        "status": "success",
                        "timestamp": "not-a-date",
                        "reference": "receipt-1",
                    },
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            validate_value(document, "flow-results.schema.json", "golden")

    def test_flow_cases_reject_duplicate_paths(self) -> None:
        document = load_json(FLOW_CASES_PATH)
        document["cases"].append(dict(document["cases"][0], name="duplicate"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flows.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "contains duplicate flow case path"):
                load_flow_cases(path)

    def test_write_flow_results_validates_before_writing(self) -> None:
        invalid_results = [{"name": "missing_outcome"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "golden-results.json"

            with self.assertRaisesRegex(ValueError, "'outcome' is a required property"):
                write_flow_results(invalid_results, path)

            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
