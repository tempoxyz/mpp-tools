#!/usr/bin/env python3
"""
Fail when the flow goldens or pair snapshots change without a reason.

flows/golden-results.json and pairs/snapshots.json record what mppx, the
reference SDK, does on the wire. They may change only with the mppx pin
(conformance/package.json) or with the fixtures and runners that produce
them; anything else is drift that would slip a wire change in unreviewed.

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
# Changes here explain a golden change: the cases, the reference adapter and
# servers that answer them, the runners that record them and their schemas.
FIXTURES = (
    "conformance/flows/",
    "conformance/pairs/scenarios.json",
    "conformance/golden/",
    "conformance/schemas/",
    "conformance/scripts/flow_runner.py",
    "conformance/scripts/pair_runner.py",
)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_DIR, check=True, capture_output=True, text=True).stdout


def mppx_pin(ref: str) -> str | None:
    try:
        manifest = json.loads(git("show", f"{ref}:{PACKAGE_JSON}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    return manifest.get("dependencies", {}).get("mppx")


def drift_errors(changed: list[str], old_mppx: str | None, new_mppx: str | None) -> list[str]:
    """Explain why the changed goldens are not covered, or return nothing."""
    goldens = [path for path in changed if path in GOLDENS]
    if not goldens:
        return []
    if old_mppx != new_mppx:
        return []
    fixtures = [path for path in changed if path not in GOLDENS and path.startswith(FIXTURES)]
    if fixtures:
        return []
    return [
        f"{path} changed, but the mppx pin in {PACKAGE_JSON} ({new_mppx}) and the fixtures did not. "
        "Regenerate it only with a change to the mppx pin, the flow or pair cases, the reference "
        "adapter or the runners (make update-flow-golden, make update-pair-snapshots)."
        for path in goldens
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on golden or snapshot changes without an mppx pin or fixture change.")
    parser.add_argument("--base", required=True, help="Base commit of the pull request")
    parser.add_argument("--head", default="HEAD", help="Head commit (default: HEAD)")
    args = parser.parse_args(argv)
    changed = git("diff", "--name-only", f"{args.base}...{args.head}").split()
    errors = drift_errors(changed, mppx_pin(args.base), mppx_pin(args.head))
    for error in errors:
        print(f"::error file={error.split()[0]}::{error}")
    if not errors:
        print("Flow goldens and pair snapshots: no unexplained changes.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
