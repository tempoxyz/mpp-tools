#!/usr/bin/env python3
"""
Fail when the flow goldens or pair snapshots change without a reason.

flows/golden-results.json and pairs/snapshots.json record what mppx, the
reference SDK, does on the wire. They may change only with the reference
packages (the mppx pin in conformance/package.json, or the mppx, viem and ox
versions conformance/package-lock.json resolves) or with the fixtures,
adapter, servers and runners that produce them; anything else is drift that
would slip a wire change in unreviewed.

The pull request is compared against its merge base with the base branch, so
a pin bump that lands on the base branch after the pull request branched
cannot explain a golden change the pull request makes.

Usage:
    python3 scripts/check_golden_drift.py --base BASE_SHA [--head HEAD_SHA]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
GOLDENS = ("conformance/flows/golden-results.json", "conformance/pairs/snapshots.json")
PACKAGE_JSON = "conformance/package.json"
PACKAGE_LOCK = "conformance/package-lock.json"
# Packages the reference adapter (golden/adapter.ts) imports: a change to the
# version the lockfile resolves for any of them can change the wire output.
REFERENCE_PACKAGES = ("mppx", "viem", "ox")
# Changes here explain a golden change: the cases, the reference adapter, its
# manifest and the servers that answer them, the runners and the harness code
# that record them, and their schemas.
FIXTURES = (
    "conformance/flows/",
    "conformance/pairs/scenarios.json",
    "conformance/golden/",
    "conformance/adapters/typescript/adapter.json",
    "conformance/schemas/",
    "conformance/scripts/flow_runner.py",
    "conformance/scripts/pair_runner.py",
    "conformance/scripts/harness.py",
    "conformance/scripts/conformance_checks.py",
)

Pins = dict[str, str | None]


def git(*args: str, repo: Path = REPO_DIR) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def read_json(ref: str, path: str, repo: Path) -> dict:
    try:
        value = json.loads(git("show", f"{ref}:{path}", repo=repo))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def reference_pins(ref: str, repo: Path = REPO_DIR) -> Pins:
    """The mppx pin and the lockfile's resolved reference package versions at ref."""
    pins: Pins = {"package.json mppx": read_json(ref, PACKAGE_JSON, repo).get("dependencies", {}).get("mppx")}
    packages = read_json(ref, PACKAGE_LOCK, repo).get("packages", {})
    for key, entry in sorted(packages.items()):
        name = key.rpartition("node_modules/")[2]
        if name in REFERENCE_PACKAGES and isinstance(entry, dict):
            pins[f"package-lock.json {key}"] = entry.get("version")
    return pins


def drift_errors(changed: list[str], old_pins: Pins, new_pins: Pins) -> list[str]:
    """Explain why the changed goldens are not covered, or return nothing."""
    goldens = [path for path in changed if path in GOLDENS]
    if not goldens:
        return []
    if old_pins != new_pins:
        return []
    fixtures = [path for path in changed if path not in GOLDENS and path.startswith(FIXTURES)]
    if fixtures:
        return []
    return [
        f"{path} changed, but the reference packages (mppx pin {new_pins.get('package.json mppx')}) and "
        "the fixtures did not. Regenerate it only with a change to the mppx pin, the resolved mppx, viem "
        "or ox version, the flow or pair cases, the reference adapter, the harness or the runners "
        "(make update-flow-golden, make update-pair-snapshots)."
        for path in goldens
    ]


def main(argv: list[str] | None = None, repo: Path = REPO_DIR) -> int:
    parser = argparse.ArgumentParser(description="Fail on golden or snapshot changes without a reference package or fixture change.")
    parser.add_argument("--base", required=True, help="Base branch commit of the pull request")
    parser.add_argument("--head", default="HEAD", help="Head commit (default: HEAD)")
    args = parser.parse_args(argv)
    merge_base = git("merge-base", args.base, args.head, repo=repo).strip()
    changed = git("diff", "--name-only", merge_base, args.head, repo=repo).split()
    errors = drift_errors(changed, reference_pins(merge_base, repo), reference_pins(args.head, repo))
    for error in errors:
        print(f"::error file={error.split()[0]}::{error}")
    if not errors:
        print("Flow goldens and pair snapshots: no unexplained changes.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
