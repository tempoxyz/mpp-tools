from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import bump_reference_pins as pins


class PinRewriteTests(unittest.TestCase):
    """The manifest rewrites, on copies of the checked-in adapter manifests."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for adapter, manifest in (
            ("python", "pyproject.toml"),
            ("rust", "Cargo.toml"),
            ("ruby", "Gemfile"),
            ("java", "build.gradle"),
        ):
            (self.tmp / "adapters" / adapter).mkdir(parents=True)
            shutil.copy(pins.ADAPTERS_DIR / adapter / manifest, self.tmp / "adapters" / adapter / manifest)
        patcher = mock.patch.multiple(pins, CONFORMANCE_DIR=self.tmp, ADAPTERS_DIR=self.tmp / "adapters")
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, path: str) -> str:
        return (self.tmp / "adapters" / path).read_text(encoding="utf-8")

    def test_python_pins_a_release_and_keeps_extras(self) -> None:
        self.assertTrue(pins._python_pin("9.9.9"))
        self.assertIn('    "pympp[tempo]==9.9.9",\n', self.read("python/pyproject.toml"))
        self.assertFalse(pins._python_pin("9.9.9"))

    def test_rust_pins_a_release_and_keeps_features(self) -> None:
        self.assertTrue(pins._rust_pin("9.9.9"))
        line = next(line for line in self.read("rust/Cargo.toml").splitlines() if line.startswith("mpp "))
        self.assertTrue(line.startswith('mpp = { version = "=9.9.9", features = ['))
        self.assertIn('"tempo"', line)
        self.assertFalse(pins._rust_pin("9.9.9"))

    def test_ruby_and_java_pin_a_release(self) -> None:
        self.assertTrue(pins._ruby_pin("9.9.9"))
        self.assertIn('gem "mpp-rb", "9.9.9"\n', self.read("ruby/Gemfile"))
        self.assertTrue(pins._java_pin("9.9.9"))
        self.assertIn("implementation 'com.stripe:mpp-java:9.9.9'\n", self.read("java/build.gradle"))

    def test_missing_pin_is_an_error(self) -> None:
        (self.tmp / "adapters" / "ruby" / "Gemfile").write_text('source "https://rubygems.org"\n', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            pins._ruby_pin("9.9.9")


class BumpTests(unittest.TestCase):
    def sdk(self, current: str, latest: str, edited: bool = False, error: Exception | None = None) -> tuple[pins.Sdk, list[str]]:
        locked: list[str] = []
        state = {"current": current}

        def lock(version: str) -> None:
            if error:
                raise error
            locked.append(version)
            state["current"] = version

        published = datetime(2026, 11, 1, tzinfo=timezone.utc)
        sdk = pins.Sdk(
            "typescript", "mppx", "npm", lambda: [(latest, published)], lambda: state["current"], lambda version: edited, lock,
        )
        return sdk, locked

    def test_bumps_and_locks_a_newer_release(self) -> None:
        sdk, locked = self.sdk("0.11.0", "0.21.1")
        with mock.patch.dict(pins.SDKS, {"typescript": sdk}), mock.patch("sys.stderr"):
            [record] = pins.bump(["typescript"])
        self.assertEqual(locked, ["0.21.1"])
        self.assertEqual(
            record,
            {"adapter": "typescript", "package": "mppx", "registry": "npm", "from": "0.11.0", "to": "0.21.1", "changed": True},
        )

    def test_current_pin_is_left_alone(self) -> None:
        sdk, locked = self.sdk("0.21.1", "0.21.1")
        with mock.patch.dict(pins.SDKS, {"typescript": sdk}), mock.patch("sys.stderr"):
            [record] = pins.bump(["typescript"])
        self.assertEqual(locked, [])
        self.assertFalse(record["changed"])

    def test_git_pin_of_the_same_version_still_relocks(self) -> None:
        sdk, locked = self.sdk("0.21.1", "0.21.1", edited=True)
        with mock.patch.dict(pins.SDKS, {"typescript": sdk}), mock.patch("sys.stderr"):
            [record] = pins.bump(["typescript"])
        self.assertEqual(locked, ["0.21.1"])
        self.assertTrue(record["changed"])

    def test_pins_never_move_back(self) -> None:
        sdk, locked = self.sdk("0.22.0", "0.21.1")
        with mock.patch.dict(pins.SDKS, {"typescript": sdk}), mock.patch("sys.stderr"):
            [record] = pins.bump(["typescript"])
        self.assertEqual(locked, [])
        self.assertEqual(record["to"], "0.22.0")
        self.assertFalse(record["changed"])

    def test_errors_are_recorded_per_sdk(self) -> None:
        sdk, _ = self.sdk("0.11.0", "0.21.1", error=RuntimeError("npm is not installed"))
        with mock.patch.dict(pins.SDKS, {"typescript": sdk}), mock.patch("sys.stderr"):
            [record] = pins.bump(["typescript"])
        self.assertEqual(record["error"], "npm is not installed")


class VersionTests(unittest.TestCase):
    def test_pick_release_skips_prereleases_and_too_new_releases(self) -> None:
        now = datetime(2026, 11, 21, tzinfo=timezone.utc)
        releases = [
            ("0.20.1", now - timedelta(days=5)),
            ("0.21.0", now - timedelta(days=3)),
            ("0.9.0", now - timedelta(days=30)),
            ("0.22.0-beta.1", now - timedelta(days=4)),
            ("0.21.1", now - timedelta(hours=10)),
        ]
        self.assertEqual(pins.pick_release(releases, now - timedelta(days=2)), ("0.21.0", "0.21.1"))
        self.assertEqual(pins.pick_release(releases, now), ("0.21.1", "0.21.1"))
        with self.assertRaises(RuntimeError):
            pins.pick_release(releases, now - timedelta(days=60))

    def test_go_style_versions_compare_numerically(self) -> None:
        now = datetime(2026, 11, 21, tzinfo=timezone.utc)
        self.assertEqual(pins.pick_release([("v0.10.0", now), ("v0.9.1", now)], now), ("v0.10.0", "v0.10.0"))

    def test_satisfies_minimum(self) -> None:
        self.assertTrue(pins.satisfies_minimum("2.58.4", ">=2.57.19"))
        self.assertFalse(pins.satisfies_minimum("2.57.1", ">=2.57.19"))
        self.assertTrue(pins.satisfies_minimum("2.58.0", "^2.57.0"))
        self.assertFalse(pins.satisfies_minimum("3.0.0", "^2.57.0"))
        self.assertFalse(pins.satisfies_minimum("2.58.0", "2.x || 3.x"))


class SummaryTests(unittest.TestCase):
    before = {
        "results": [
            {"name": "success_charge", "outcome": {"ok": True, "status": 200}, "receipt": {"status": "success"}},
            {"name": "expired", "outcome": {"ok": False, "status": 402}},
        ]
    }
    after = {
        "results": [
            {"name": "success_charge", "outcome": {"ok": True, "status": 200}, "receipt": {"status": "ok"}},
            {"name": "new_case", "outcome": {"ok": True, "status": 200}},
        ]
    }

    def test_diff_cases_names_changed_fields(self) -> None:
        path = "flows/golden-results.json"
        diff = pins.diff_cases(pins.golden_cases(path, self.before), pins.golden_cases(path, self.after))
        self.assertEqual(diff, {"added": ["new_case"], "removed": ["expired"], "changed": {"success_charge": ["receipt.status"]}})

    def test_changed_paths_name_nested_fields(self) -> None:
        before = {"challenge": {"id": "a", "realm": "r"}, "problem_details": {"type": "x", "status": 402}}
        after = {"challenge": {"id": "<nonce-bound>", "realm": "r"}, "problem_details": {"type": "y", "status": 500}, "new": 1}
        self.assertEqual(
            pins.changed_paths(before, after),
            ["challenge.id", "new", "problem_details.status", "problem_details.type"],
        )
        self.assertEqual(pins.changed_paths({"a": {"b": {"c": {"d": 1}}}}, {"a": {"b": {"c": {"d": 2}}}}), ["a.b.c"])

    def test_pair_snapshots_index_by_scenario(self) -> None:
        cases = pins.golden_cases("pairs/snapshots.json", {"$schema": "x", "scenarios": {"happy_path": {"steps": []}}})
        self.assertEqual(cases, {"happy_path": {"steps": []}})

    def test_render_summary(self) -> None:
        changes = [
            {"adapter": "typescript", "package": "mppx", "registry": "npm", "from": "0.11.0", "to": "0.21.1", "changed": True},
            {
                "adapter": "java", "package": "com.stripe:mpp-java", "registry": "Maven Central",
                "from": "0.1.6", "to": "0.1.6", "changed": False,
            },
            {"adapter": "ruby", "package": "mpp-rb", "registry": "RubyGems", "from": "0.1.6", "error": "bundle is not installed"},
        ]
        diffs = {
            "flows/golden-results.json": {"added": [], "removed": [], "changed": {"success_charge": ["receipt"]}},
            "pairs/snapshots.json": {"added": [], "removed": [], "changed": {}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "vectors.log"
            log.write_text(
                "  www-authenticate::TestType.PARSE::unicode_escape_in_realm_is_quoted_pair ... FAIL\n"
                "PASSED: 10 passed, 0 failed\n"
                "FAILED: 9 passed, 1 failed, 10 total\n"
                "  1) python::www-authenticate::TestType.PARSE::unicode_escape_in_realm_is_quoted_pair\n",
                encoding="utf-8",
            )
            body = pins.render_summary(changes, diffs, [("vectors", 1, log), ("flow", 0, None), ("pair", None, None)])
        self.assertIn("| typescript | `mppx` (npm) | 0.11.0 | 0.21.1 |", body)
        self.assertIn("| java | `com.stripe:mpp-java` (Maven Central) | 0.1.6 | 0.1.6 (unchanged) |", body)
        self.assertIn("not bumped: bundle is not installed", body)
        self.assertIn("  - changed `success_charge`: `receipt`", body)
        self.assertIn("- `pairs/snapshots.json`: no changes.", body)
        self.assertIn("| vectors | failed |", body)
        self.assertIn("| pair | not run |", body)
        self.assertIn("1) python::www-authenticate::TestType.PARSE::unicode_escape_in_realm_is_quoted_pair", body)
        self.assertIn("FAILED: 9 passed, 1 failed, 10 total", body)
        self.assertNotIn("... FAIL", body)
        self.assertNotIn("PASSED", body)

    def test_parse_suite(self) -> None:
        self.assertEqual(pins.parse_suite("vectors=1:logs/vectors.log"), ("vectors", 1, Path("logs/vectors.log")))
        self.assertEqual(pins.parse_suite("pair=skipped"), ("pair", None, None))


if __name__ == "__main__":
    unittest.main()
