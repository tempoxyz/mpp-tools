from __future__ import annotations

import tempfile
import unittest
from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

from agricola.manifest import ManifestError, generated_schemas, load_manifest
from agricola.models import Automation


class ManifestTests(unittest.TestCase):
    def test_repository_manifest_loads(self) -> None:
        manifest = load_manifest("sdks.yaml")
        self.assertEqual(manifest.canonical.repo, "wevm/mppx")
        self.assertEqual(manifest.sdks["go"].automation, Automation.PR)
        self.assertEqual(manifest.sdks["ruby"].automation, Automation.NOTIFY)

    def test_rejects_pr_target_without_verification(self) -> None:
        self.assert_invalid(
            """
version: 1
maintainers: [maintainer]
canonical: {repo: wevm/mppx}
spec: {repo: tempoxyz/mpp-specs}
sdks:
  go:
    repo: tempoxyz/mpp-go
    automation: pr
    owners: []
    changelog: keep-a-changelog
    verify: []
    capabilities: []
""",
            "verify must not be empty",
        )

    def test_rejects_unknown_fields(self) -> None:
        self.assert_invalid(
            """
version: 1
maintainers: [maintainer]
canonical: {repo: wevm/mppx}
spec: {repo: tempoxyz/mpp-specs}
sdks: {}
database: postgres
""",
            "database: Extra inputs are not permitted",
        )

    def test_rejects_duplicate_repository(self) -> None:
        self.assert_invalid(
            """
version: 1
maintainers: [maintainer]
canonical: {repo: wevm/mppx}
spec: {repo: tempoxyz/mpp-specs}
sdks:
  typescript:
    repo: wevm/mppx
    automation: notify
    owners: []
    changelog: none
    verify: []
    capabilities: []
""",
            "appears more than once",
        )

    def test_rejects_duplicate_yaml_keys(self) -> None:
        self.assert_invalid(
            """
version: 1
maintainers: [maintainer]
canonical: {repo: wevm/mppx}
spec: {repo: tempoxyz/mpp-specs}
sdks:
  go:
    repo: tempoxyz/mpp-go
    automation: pr
    owners: []
    changelog: keep-a-changelog
    verify: [make test]
    capabilities: []
  go:
    repo: example/mpp-go
    automation: notify
    owners: []
    changelog: none
    verify: []
    capabilities: []
""",
            "duplicate key: go",
        )

    def test_sdk_mapping_is_immutable(self) -> None:
        manifest = load_manifest("sdks.yaml")
        sdks = cast(MutableMapping, manifest.sdks)
        with self.assertRaises(TypeError):
            sdks["new"] = manifest.sdks["go"]

    def test_generated_schemas_cover_persisted_models(self) -> None:
        schemas = generated_schemas()
        self.assertEqual(set(schemas), {"manifest", "ledger", "cursor"})
        self.assertFalse(schemas["manifest"]["additionalProperties"])
        ledger_definitions = schemas["ledger"]["$defs"]
        self.assertIsInstance(ledger_definitions, dict)
        assert isinstance(ledger_definitions, dict)
        self.assertIn("PropagateDecision", ledger_definitions)
        self.assertIn("SkipDecision", ledger_definitions)
        self.assertEqual(
            schemas["cursor"]["required"],
            ["merged_at"],
        )

    def assert_invalid(self, text: str, message: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sdks.yaml"
            path.write_text(text)
            with self.assertRaisesRegex(ManifestError, message):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
