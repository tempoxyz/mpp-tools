"""Tests for the Tempo conformance runner's skip rules and SDK version readers.

Run: make test-scripts (python -m unittest discover -s scripts -p "test_*.py")
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run  # noqa: E402
import sdk_version  # noqa: E402

ADAPTERS = {"go", "python", "rust", "typescript"}


class FakeAdapter:
    def __init__(self, name: str, version: str, capabilities=("tx.sign",), gaps=None):
        self.name = name
        self.version = version
        self.capabilities = set(capabilities)
        self.gaps = gaps or {}


class CheckSkipsTest(unittest.TestCase):
    def test_reasons_must_match_skips(self) -> None:
        run.check_skips({"sdkVersions": {"go": ">0.6.1"}, "skipReasons": {"go": "why"}}, ADAPTERS, "s")
        run.check_skips({"adapters": ["rust", "typescript"], "skipReasons": {"go": "a", "python": "b"}}, ADAPTERS, "s")
        with self.assertRaisesRegex(ValueError, "missing \\['go'\\]"):
            run.check_skips({"sdkVersions": {"go": ">0.6.1"}}, ADAPTERS, "s")
        with self.assertRaisesRegex(ValueError, "extra \\['rust'\\]"):
            run.check_skips({"sdkVersions": {"go": ">0.6.1"}, "skipReasons": {"go": "a", "rust": "b"}}, ADAPTERS, "s")
        with self.assertRaisesRegex(ValueError, "missing \\['python'\\]"):
            run.check_skips({"adapters": ["go", "rust", "typescript"]}, ADAPTERS, "s")

    def test_unknown_adapter_and_bad_constraint(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown adapter"):
            run.check_skips({"sdkVersions": {"java": ">1.0.0"}, "skipReasons": {"java": "x"}}, ADAPTERS, "s")
        with self.assertRaisesRegex(ValueError, "sdkVersions.go"):
            run.check_skips({"sdkVersions": {"go": "newer please"}, "skipReasons": {"go": "x"}}, ADAPTERS, "s")


class SkipReasonTest(unittest.TestCase):
    scenario = {"sdkVersions": {"go": ">0.6.1"}, "skipReasons": {"go": "fixed by tempo-go#135"}}

    def test_version_constraint(self) -> None:
        kind, detail = run.skip_reason(self.scenario, FakeAdapter("go", "v0.6.1"), "tx.sign")
        self.assertEqual(kind, "sdkVersions")
        self.assertIn("tempo-go#135", detail)
        self.assertIsNone(run.skip_reason(self.scenario, FakeAdapter("go", "v0.7.0"), "tx.sign"))
        # npm semantics: a pre-release (such as a Go pseudo-version of an unreleased
        # commit) never satisfies a plain range, so test an unreleased SDK with --sdk-version.
        self.assertIsNotNone(run.skip_reason(self.scenario, FakeAdapter("go", "v0.6.2-0.20261031000000-542c18e3830b"), "tx.sign"))
        self.assertIsNone(run.skip_reason(self.scenario, FakeAdapter("python", "0.6.2"), "tx.sign"))

    def test_adapters_and_capabilities(self) -> None:
        s = {"adapters": ["rust"], "skipReasons": {"go": "g", "python": "p", "typescript": "t"}}
        self.assertEqual(run.skip_reason(s, FakeAdapter("go", "v1.0.0"), "tx.sign"), ("adapters", "g"))
        self.assertIsNone(run.skip_reason(s, FakeAdapter("rust", "1.11.0"), "tx.sign"))
        a = FakeAdapter("go", "v1.0.0", gaps={"signature.recover": "no API"})
        self.assertEqual(run.skip_reason({}, a, "signature.recover")[0], "capability")


class CompareTest(unittest.TestCase):
    def test_errors_and_values(self) -> None:
        self.assertTrue(run.compare({"ok": False, "error": {"type": "decode_error"}}, {"ok": False, "error": {"type": "decode_error", "message": "x"}})[0])
        self.assertFalse(run.compare({"ok": False, "error": {"type": "decode_error"}}, {"ok": True, "value": {}})[0])
        ok, detail = run.compare({"ok": True, "value": {"a": "1"}}, {"ok": True, "value": {"a": "2", "b": None}})
        self.assertFalse(ok)
        self.assertIn(".b: unexpected key", detail)


class SdkVersionTest(unittest.TestCase):
    def test_readers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/ox": {"version": "0.14.59"}}}))
            self.assertEqual(sdk_version.READERS["package-lock.json"](p / "package-lock.json", "ox"), "0.14.59")
            (p / "uv.lock").write_text('[[package]]\nname = "pytempo"\nversion = "0.6.2"\n')
            self.assertEqual(sdk_version.READERS["uv.lock"](p / "uv.lock", "pytempo"), "0.6.2")
            (p / "go.mod").write_text("module x\n\nrequire (\n\tgithub.com/tempoxyz/tempo-go v0.6.1\n)\n")
            self.assertEqual(sdk_version.READERS["go.mod"](p / "go.mod", "github.com/tempoxyz/tempo-go"), "v0.6.1")
            (p / "go.mod").write_text("module x\nrequire github.com/tempoxyz/tempo-go v0.6.1\nreplace github.com/tempoxyz/tempo-go => ../tempo-go\n")
            with self.assertRaisesRegex(RuntimeError, "local path"):
                sdk_version.READERS["go.mod"](p / "go.mod", "github.com/tempoxyz/tempo-go")

    def test_pinned_adapters(self) -> None:
        for name in ADAPTERS:
            self.assertRegex(sdk_version.installed_version(name), r"^v?\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
