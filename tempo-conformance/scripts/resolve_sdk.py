#!/usr/bin/env python3
"""Resolve each adapter's SDK to its newest release unless it is pinned.

By default the suite tests the latest release of every SDK: before building,
the runner rewrites an adapter's manifest and lockfile to the newest version
of its SDK package (adapter.json sdk.package) and restores both files when it
finishes. An adapter stays on its checked-in lockfile when it is pinned:
--sdk pinned (or TEMPO_CONFORMANCE_SDK=pinned) for all, --pin ADAPTER for one,
or "pinned": true in its adapter.json sdk block. If resolving fails (a registry
or the CI package proxy refuses a just-published release), the adapter falls
back to its checked-in pin and the run says so.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

# Where --keep-resolved keeps the checked-in files, so the next run restores them.
STASH = ".sdk-pinned"


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> str:
    out = subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _go(cwd: Path, package: str, env: dict[str, str]) -> None:
    _run(["go", "get", f"{package}@latest"], cwd, {**env, "GOFLAGS": "-mod=mod"})
    _run(["go", "mod", "tidy"], cwd, env)


def _uv(cwd: Path, package: str, env: dict[str, str]) -> None:
    # Lift the requirement's upper bound, then lock the newest release.
    _run(["uv", "add", "--no-sync", "--upgrade-package", package, f"{package}>=0"], cwd, env)


def _cargo(cwd: Path, package: str, env: dict[str, str]) -> None:
    found = _run(["cargo", "search", package, "--limit", "1", "--color", "never"], cwd, env)
    match = re.match(rf'^{re.escape(package)} = "([^"]+)"', found)
    if not match:
        raise RuntimeError(f"cargo search did not report {package}: {found!r}")
    version = match.group(1)
    if "-" in version:  # cargo search reports pre-releases; test the newest stable one, as go, uv and npm do
        version = _crates_io_stable(package)
    # cargo add on an existing dependency keeps its features.
    _run(["cargo", "add", f"{package}@={version}"], cwd, env)


def _crates_io_stable(package: str) -> str:
    req = urllib.request.Request(f"https://crates.io/api/v1/crates/{package}",
                                 headers={"User-Agent": "tempoxyz/mpp-tools tempo-conformance"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        version = json.load(resp)["crate"].get("max_stable_version")
    if not version:
        raise RuntimeError(f"crates.io lists no stable release of {package}")
    return version


def _npm(cwd: Path, package: str, env: dict[str, str]) -> None:
    # The TypeScript reference is viem's bundled ox: take the newest viem and
    # the exact ox it depends on, so the two never diverge.
    if package != "ox":
        _run(["npm", "install", "--save-exact", "--no-audit", "--no-fund", f"{package}@latest"], cwd, env)
        return
    viem = _run(["npm", "view", "viem", "version"], cwd, env)
    ox = json.loads(_run(["npm", "view", f"viem@{viem}", "dependencies", "--json"], cwd, env))["ox"]
    _run(["npm", "install", "--save-exact", "--no-audit", "--no-fund", f"viem@{viem}", f"ox@{ox}"], cwd, env)


RESOLVERS = {"go.mod": _go, "uv.lock": _uv, "Cargo.lock": _cargo, "package-lock.json": _npm}


class Resolution:
    """Rewrites one adapter to its SDK's latest release; restore() undoes it."""

    def __init__(self, adapter_dir: Path, sdk: dict):
        self.dir = adapter_dir
        self.sdk = sdk
        files = {sdk["manifest"], sdk["lockfile"]} | ({"go.sum"} if sdk["lockfile"] == "go.mod" else set())
        self.files = sorted(self.dir / f for f in files)
        self.backups: dict[Path, bytes | None] = {}

    def apply(self, env: dict[str, str]) -> None:
        resolver = RESOLVERS.get(self.sdk["lockfile"])
        if resolver is None:
            raise RuntimeError(f"no latest-version resolver for {self.sdk['lockfile']}")
        self.backups = {f: (f.read_bytes() if f.exists() else None) for f in self.files}
        try:
            resolver(self.dir, self.sdk["package"], env)
        except subprocess.CalledProcessError as error:
            self.restore()
            detail = (error.stderr or error.stdout or "").strip().splitlines()
            raise RuntimeError(f"{' '.join(error.cmd)}: {detail[-1] if detail else error}") from error
        except Exception:
            self.restore()
            raise

    def stash(self) -> None:
        """Keep the resolved files in place (--keep-resolved) but save the
        checked-in ones, which restore_stashed() puts back on the next run."""
        stash = self.dir / STASH
        stash.mkdir(exist_ok=True)
        for f, data in self.backups.items():
            (stash / (f.name + (".absent" if data is None else ""))).write_bytes(data or b"")
        self.backups = {}

    def restore(self) -> None:
        for f, data in self.backups.items():
            if data is None:
                f.unlink(missing_ok=True)
            else:
                f.write_bytes(data)
        self.backups = {}



def restore_stashed(adapter_dir: Path) -> bool:
    """Put back the checked-in files an earlier --keep-resolved run left
    rewritten, so this run resolves (or stays pinned) from the real pin."""
    stash = adapter_dir / STASH
    if not stash.is_dir():
        return False
    for saved in stash.iterdir():
        if saved.name.endswith(".absent"):
            (adapter_dir / saved.name.removesuffix(".absent")).unlink(missing_ok=True)
        else:
            shutil.copyfile(saved, adapter_dir / saved.name)
    shutil.rmtree(stash)
    return True
