from __future__ import annotations

import unittest

from jsonschema import Draft202012Validator

from harness import (
    FLOW_CASES_PATH,
    FLOW_RESULTS_PATH,
    SCHEMA_STORE,
    VECTORS_DIR,
    load_flow_cases,
    load_flow_results,
    load_json,
    load_vector,
    validate_value,
)


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


if __name__ == "__main__":
    unittest.main()
