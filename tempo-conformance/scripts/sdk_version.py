#!/usr/bin/env python3
"""Print the pinned version of the SDK an adapter tests, read from its lockfile.

Each adapter.json names its SDK package and the lockfile that pins it
(sdk.package, sdk.lockfile). sdkVersions constraints in vectors/*.json are
evaluated against this version, the way ../conformance/scripts/sdk_version.py
does for the MPP adapters. Reading lockfiles needs no toolchain, so the runner
can evaluate constraints before (or without) building an adapter.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _npm(lockfile: Path, package: str) -> str:
    data = json.loads(lockfile.read_text())
    entry = data.get("packages", {}).get(f"node_modules/{package}")
    if not entry or "version" not in entry:
        raise RuntimeError(f"{package} is not in {lockfile}")
    return entry["version"]


def _toml_packages(lockfile: Path, package: str) -> str:
    """Cargo.lock and uv.lock: [[package]] tables with name and version."""
    data = tomllib.loads(lockfile.read_text())
    versions = sorted({p["version"] for p in data.get("package", []) if p.get("name") == package})
    if len(versions) != 1:
        found = ", ".join(versions) or "none"
        raise RuntimeError(f"expected exactly one {package} in {lockfile}, found {found}")
    return versions[0]


def _gomod(lockfile: Path, package: str) -> str:
    """go.mod: the required version of the module, refusing local replaces."""
    text = lockfile.read_text()
    # Replace directives, single-line (replace a => b) and block form (replace ( a => b )).
    directives = re.findall(r"^\s*replace\s+(?!\()(.+?=>.+)$", text, re.MULTILINE)
    for block in re.findall(r"^\s*replace\s*\((.*?)^\s*\)", text, re.MULTILINE | re.DOTALL):
        directives += [line.strip() for line in block.splitlines() if "=>" in line]
    for directive in directives:
        match = re.match(r"^(\S+)(?:\s+\S+)?\s+=>\s+(\S+)(?:\s+(\S+))?", directive.split("//")[0].strip())
        if match and match.group(1) == package:
            if match.group(3):
                return match.group(3)
            raise RuntimeError(f"{package} is replaced by a local path in {lockfile}; its version is unknown")
    match = re.search(rf"^\s*(?:require\s+)?{re.escape(package)}\s+(v\S+)", text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"{package} is not required in {lockfile}")
    return match.group(1)


READERS = {
    "package-lock.json": _npm,
    "Cargo.lock": _toml_packages,
    "uv.lock": _toml_packages,
    "go.mod": _gomod,
}


def manifest(adapter: str) -> dict:
    path = ROOT / "adapters" / adapter / "adapter.json"
    if not path.exists():
        raise RuntimeError(f"no adapter {adapter!r} ({path} is missing)")
    return json.loads(path.read_text())


def sdk_summary(adapter: str) -> str:
    """package@version, as pinned for this adapter."""
    sdk = manifest(adapter)["sdk"]
    lockfile = ROOT / "adapters" / adapter / sdk["lockfile"]
    reader = READERS.get(lockfile.name)
    if reader is None:
        raise RuntimeError(f"{adapter}: no reader for lockfile {lockfile.name}")
    return f"{sdk['package']}@{reader(lockfile, sdk['package'])}"


def installed_version(adapter: str) -> str:
    """Only the version, for constraint evaluation (a leading v is kept; the
    constraint matcher strips it)."""
    _, sep, version = sdk_summary(adapter).rpartition("@")
    if not sep or not version:
        raise RuntimeError(f"could not parse the SDK version of {adapter}")
    return version


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adapter", help="one adapter (default: all)")
    args = ap.parse_args()
    names = [args.adapter] if args.adapter else sorted(p.parent.name for p in (ROOT / "adapters").glob("*/adapter.json"))
    for name in names:
        print(f"{name}: {sdk_summary(name)}" if not args.adapter else sdk_summary(name))
    return 0


if __name__ == "__main__":
    sys.exit(main())

