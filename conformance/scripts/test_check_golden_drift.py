from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from check_golden_drift import drift_errors, main, reference_pins

GOLDEN = "conformance/flows/golden-results.json"
SNAPSHOTS = "conformance/pairs/snapshots.json"


def pins(mppx: str, viem: str = "2.57.1", ox: str = "0.14.45") -> dict[str, str | None]:
    return {
        "package.json mppx": mppx,
        "package-lock.json node_modules/mppx": mppx,
        "package-lock.json node_modules/viem": viem,
        "package-lock.json node_modules/ox": ox,
    }


class GoldenDriftTests(unittest.TestCase):
    def test_no_golden_change_passes(self) -> None:
        self.assertEqual(drift_errors(["conformance/vectors/receipt.json"], pins("0.11.0"), pins("0.11.0")), [])

    def test_golden_change_alone_fails(self) -> None:
        errors = drift_errors([GOLDEN, SNAPSHOTS, "README.md"], pins("0.11.0"), pins("0.11.0"))
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith(GOLDEN))
        self.assertTrue(errors[1].startswith(SNAPSHOTS))

    def test_mppx_pin_change_explains_goldens(self) -> None:
        self.assertEqual(drift_errors([GOLDEN, "conformance/package.json"], pins("0.11.0"), pins("0.21.1")), [])

    def test_lockfile_only_change_does_not_explain_goldens(self) -> None:
        changed = [GOLDEN, "conformance/package-lock.json", "conformance/package.json"]
        self.assertNotEqual(drift_errors(changed, pins("0.11.0"), pins("0.11.0")), [])

    def test_reference_package_bump_explains_goldens(self) -> None:
        for new in (pins("0.11.0", viem="2.58.0"), pins("0.11.0", ox="0.14.50")):
            with self.subTest(new=new):
                self.assertEqual(drift_errors([GOLDEN, "conformance/package-lock.json"], pins("0.11.0"), new), [])

    def test_fixture_changes_explain_goldens(self) -> None:
        for fixture in (
            "conformance/flows/flows.json",
            "conformance/flows/compliance-server.ts",
            "conformance/pairs/scenarios.json",
            "conformance/golden/adapter.ts",
            "conformance/adapters/typescript/adapter.json",
            "conformance/scripts/flow_runner.py",
            "conformance/scripts/pair_runner.py",
            "conformance/scripts/harness.py",
            "conformance/scripts/conformance_checks.py",
        ):
            with self.subTest(fixture=fixture):
                self.assertEqual(drift_errors([SNAPSHOTS, fixture], pins("0.11.0"), pins("0.11.0")), [])

    def test_unrelated_script_does_not_explain_goldens(self) -> None:
        changed = [GOLDEN, "conformance/scripts/vector_runner.py"]
        self.assertNotEqual(drift_errors(changed, pins("0.11.0"), pins("0.11.0")), [])

    def test_one_golden_does_not_explain_the_other(self) -> None:
        self.assertNotEqual(drift_errors([GOLDEN, SNAPSHOTS], pins("0.11.0"), pins("0.11.0")), [])


class MergeBaseTests(unittest.TestCase):
    """Run the check against a throwaway repository with diverged branches."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.write_manifests("0.11.0")
        self.write(GOLDEN, "[]")
        self.commit("initial")
        self.root = self.git("rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        config = ["-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
        result = subprocess.run(["git", *config, *args], cwd=self.repo, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def write_manifests(self, mppx: str) -> None:
        self.write("conformance/package.json", json.dumps({"dependencies": {"mppx": mppx, "viem": "2.57.1"}}))
        lock = {"packages": {"": {}, "node_modules/mppx": {"version": mppx}, "node_modules/viem": {"version": "2.57.1"}}}
        self.write("conformance/package-lock.json", json.dumps(lock))

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_check(self, base: str, head: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["--base", base, "--head", head], repo=self.repo)

    def test_reads_lockfile_reference_versions(self) -> None:
        self.assertEqual(
            reference_pins("HEAD", self.repo),
            {
                "package.json mppx": "0.11.0",
                "package-lock.json node_modules/mppx": "0.11.0",
                "package-lock.json node_modules/viem": "2.57.1",
            },
        )

    def test_base_branch_pin_bump_does_not_explain_branch_golden_change(self) -> None:
        self.write_manifests("0.12.0")
        self.commit("bump mppx on main")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "feature", self.root)
        self.write(GOLDEN, '[{"changed": true}]')
        self.commit("golden only")
        self.assertEqual(self.run_check(base, "HEAD"), 1)

    def test_branch_pin_bump_explains_its_golden_change(self) -> None:
        self.git("checkout", "-q", "-b", "feature")
        self.write_manifests("0.12.0")
        self.write(GOLDEN, '[{"changed": true}]')
        self.commit("bump mppx and regenerate")
        self.assertEqual(self.run_check("main", "HEAD"), 0)


if __name__ == "__main__":
    unittest.main()
