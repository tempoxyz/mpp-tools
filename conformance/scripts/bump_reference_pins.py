#!/usr/bin/env python3
"""
Bump the reference SDK pins to each SDK's latest release.

`bump` resolves the newest release of every SDK an adapter pins (mppx on npm,
pympp on PyPI, mpp on crates.io, mpp-go on the Go module proxy, mpp-rb on
RubyGems, mpp-java on Maven Central), rewrites the pin and its lockfile, and
writes the version changes as JSON. `summary` turns those changes, the flow
golden and pair snapshot diffs against a base commit, and the suite results
into the pull request body, so a change in mppx's wire behavior shows up as
named cases to review.

The Bump reference SDK pins workflow runs both; by hand:

    uv run --locked python scripts/bump_reference_pins.py bump --changes pins.json
    make install update-flow-golden update-pair-snapshots test flow pair
    uv run --locked python scripts/bump_reference_pins.py summary --changes pins.json --base HEAD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CONFORMANCE_DIR = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = CONFORMANCE_DIR / "adapters"
GOLDEN_PATHS = {
    "flows/golden-results.json": "flow golden results",
    "pairs/snapshots.json": "pair snapshots",
}
USER_AGENT = "tempoxyz/mpp-tools bump-reference-pins"
# GitHub rejects pull request bodies over 65,536 characters.
MAX_BODY = 60_000


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_json(url: str) -> Any:
    return json.loads(fetch(url))


def npm_latest(package: str) -> str:
    return fetch_json(f"https://registry.npmjs.org/{package}")["dist-tags"]["latest"]


def pypi_latest(package: str) -> str:
    return fetch_json(f"https://pypi.org/pypi/{package}/json")["info"]["version"]


def crates_latest(package: str) -> str:
    version = fetch_json(f"https://crates.io/api/v1/crates/{package}")["crate"].get("max_stable_version")
    if not version:
        raise RuntimeError(f"crates.io lists no stable release of {package}")
    return version


def go_latest(module: str) -> str:
    return fetch_json(f"https://proxy.golang.org/{module.lower()}/@latest")["Version"]


def rubygems_latest(gem: str) -> str:
    return fetch_json(f"https://rubygems.org/api/v1/gems/{gem}.json")["version"]


def maven_latest(group: str, artifact: str) -> str:
    path = group.replace(".", "/")
    root = ElementTree.fromstring(fetch(f"https://repo1.maven.org/maven2/{path}/{artifact}/maven-metadata.xml"))
    release = root.findtext("versioning/release")
    if not release:
        raise RuntimeError(f"Maven Central lists no release of {group}:{artifact}")
    return release


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    if not command[0].startswith("./") and shutil.which(command[0]) is None:
        raise RuntimeError(f"{command[0]} is not installed")
    print(f"  $ {' '.join(command)}  (in {cwd.relative_to(CONFORMANCE_DIR)})", file=sys.stderr, flush=True)
    result = subprocess.run(command, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail[-2000:]}")


def replace_line(path: Path, pattern: str, replacement: str) -> bool:
    """Replace the one line matching pattern; return whether the file changed."""
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"{path.relative_to(CONFORMANCE_DIR)}: no line matches {pattern!r}")
    path.write_text(updated, encoding="utf-8")
    return updated != text


def lock_version(path: Path, pattern: str) -> str | None:
    if not path.exists():
        return None
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def toml_package_version(path: Path, name: str) -> str | None:
    """The version of a [[package]] in a Cargo.lock or uv.lock."""
    return lock_version(path, rf'^name = "{re.escape(name)}"\nversion = "([^"]+)"')


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0])[:3])


def satisfies_minimum(version: str, requirement: str) -> bool:
    """Whether version meets an npm range of the form >=x.y.z or ^x.y.z; any other range reads as unmet."""
    match = re.fullmatch(r"\s*(>=|\^)\s*v?(\d+(?:\.\d+){0,2})\s*", requirement)
    if not match:
        return False
    operator, minimum = match.groups()
    have, need = _version_tuple(version), _version_tuple(minimum)
    if operator == "^" and have[:1] != need[:1]:
        return False
    return have >= need


@dataclass(frozen=True)
class Sdk:
    """One adapter's reference SDK: where its releases come from and how to pin one."""

    adapter: str
    package: str
    registry: str
    latest: Callable[[], str]
    current: Callable[[], str | None]
    # Rewrites the manifest's pin; returns whether it changed.
    pin: Callable[[str], bool]
    # Updates the lockfile to the manifest's pin.
    lock: Callable[[str], None]


def _typescript_current() -> str | None:
    lock = json.loads((CONFORMANCE_DIR / "package-lock.json").read_text(encoding="utf-8"))
    return lock.get("packages", {}).get("node_modules/mppx", {}).get("version")


def _no_manifest_edit(version: str) -> bool:
    """The package manager rewrites the manifest and the lockfile together."""
    return False


def _typescript_lock(version: str) -> None:
    dependencies = json.loads((CONFORMANCE_DIR / "package.json").read_text(encoding="utf-8"))["dependencies"]
    packages = [f"mppx@{version}"]
    # mppx declares viem as a peer: keep the exact viem pin inside its range.
    viem_range = fetch_json(f"https://registry.npmjs.org/mppx/{version}").get("peerDependencies", {}).get("viem")
    if viem_range and not satisfies_minimum(dependencies.get("viem", "0"), viem_range):
        packages.append(f"viem@{npm_latest('viem')}")
    run(["npm", "install", "--save-exact", "--no-audit", "--no-fund", *packages], CONFORMANCE_DIR)


def _python_pin(version: str) -> bool:
    return replace_line(
        ADAPTERS_DIR / "python" / "pyproject.toml",
        r'^(\s*)"pympp(\[[^\]]*\])?[^"]*",\s*$',
        rf'\1"pympp\2=={version}",',
    )


def _python_lock(version: str) -> None:
    run(["uv", "lock", "--upgrade-package", "pympp"], ADAPTERS_DIR / "python")


def _rust_pin(version: str) -> bool:
    manifest = ADAPTERS_DIR / "rust" / "Cargo.toml"
    features = re.search(r"^mpp\s*=.*features\s*=\s*(\[[^\]]*\])", manifest.read_text(encoding="utf-8"), re.MULTILINE)
    feature_list = f", features = {features.group(1)}" if features else ""
    return replace_line(manifest, r"^mpp\s*=\s*.+$", f'mpp = {{ version = "={version}"{feature_list} }}')


def _rust_lock(version: str) -> None:
    # Re-resolves mpp and only what its new requirement needs.
    run(["cargo", "update", "--package", "mpp"], ADAPTERS_DIR / "rust")


def _go_lock(version: str) -> None:
    adapter = ADAPTERS_DIR / "go"
    run(["go", "get", f"github.com/tempoxyz/mpp-go@{version}"], adapter, {"GOTOOLCHAIN": "auto", "GOFLAGS": "-mod=mod"})
    run(["go", "mod", "tidy"], adapter, {"GOTOOLCHAIN": "auto"})


def _ruby_pin(version: str) -> bool:
    return replace_line(ADAPTERS_DIR / "ruby" / "Gemfile", r"""^gem\s+["']mpp-rb["'].*$""", f'gem "mpp-rb", "{version}"')


def _ruby_lock(version: str) -> None:
    run(["bundle", "lock", "--update", "mpp-rb"], ADAPTERS_DIR / "ruby")


def _java_pin(version: str) -> bool:
    return replace_line(
        ADAPTERS_DIR / "java" / "build.gradle",
        r"""^(\s*implementation\s+['"]com\.stripe:mpp-java:)[^'"]+(['"].*)$""",
        rf"\g<1>{version}\g<2>",
    )


def _java_lock(version: str) -> None:
    run(["./gradlew", "--no-daemon", "--quiet", "dependencies", "--write-locks"], ADAPTERS_DIR / "java")


SDKS: dict[str, Sdk] = {
    sdk.adapter: sdk
    for sdk in (
        Sdk("typescript", "mppx", "npm", lambda: npm_latest("mppx"), _typescript_current, _no_manifest_edit, _typescript_lock),
        Sdk(
            "python", "pympp", "PyPI", lambda: pypi_latest("pympp"),
            lambda: toml_package_version(ADAPTERS_DIR / "python" / "uv.lock", "pympp"), _python_pin, _python_lock,
        ),
        Sdk(
            "rust", "mpp", "crates.io", lambda: crates_latest("mpp"),
            lambda: toml_package_version(ADAPTERS_DIR / "rust" / "Cargo.lock", "mpp"), _rust_pin, _rust_lock,
        ),
        Sdk(
            "go", "github.com/tempoxyz/mpp-go", "Go module proxy", lambda: go_latest("github.com/tempoxyz/mpp-go"),
            lambda: lock_version(ADAPTERS_DIR / "go" / "go.mod", r"^\s*github\.com/tempoxyz/mpp-go\s+(\S+)"),
            _no_manifest_edit, _go_lock,
        ),
        Sdk(
            "ruby", "mpp-rb", "RubyGems", lambda: rubygems_latest("mpp-rb"),
            lambda: lock_version(ADAPTERS_DIR / "ruby" / "Gemfile.lock", r"^    mpp-rb \(([^)]+)\)"), _ruby_pin, _ruby_lock,
        ),
        Sdk(
            "java", "com.stripe:mpp-java", "Maven Central", lambda: maven_latest("com.stripe", "mpp-java"),
            lambda: lock_version(ADAPTERS_DIR / "java" / "gradle.lockfile", r"^com\.stripe:mpp-java:([^=]+)="),
            _java_pin, _java_lock,
        ),
    )
}


def bump(adapters: list[str]) -> list[dict[str, Any]]:
    """Pin each adapter's SDK to its latest release; return one record per adapter."""
    records = []
    for name in adapters:
        sdk = SDKS[name]
        record: dict[str, Any] = {"adapter": name, "package": sdk.package, "registry": sdk.registry}
        try:
            record["from"] = sdk.current()
            record["to"] = sdk.latest()
            print(f"→ {name}: {sdk.package} {record['from']} -> {record['to']}", file=sys.stderr, flush=True)
            edited = sdk.pin(record["to"])
            # Lock whenever the lockfile is behind, so a rerun finishes a failed lock.
            if edited or sdk.current() != record["to"]:
                sdk.lock(record["to"])
            record["changed"] = record["from"] != record["to"] or edited
        except Exception as error:  # Report every SDK, then fail.
            record["error"] = str(error)
            print(f"✗ {name}: {error}", file=sys.stderr, flush=True)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def git_show(ref: str, path: str) -> Any:
    result = subprocess.run(["git", "show", f"{ref}:./{path}"], cwd=CONFORMANCE_DIR, capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else None


def golden_cases(path: str, document: Any) -> dict[str, Any]:
    """Index a golden file by case name."""
    if not isinstance(document, dict):
        return {}
    if path == "flows/golden-results.json":
        return {case.get("name", str(index)): case for index, case in enumerate(document.get("results", []))}
    return dict(document.get("scenarios", {}))


_MISSING = object()


def changed_paths(left: Any, right: Any, prefix: str = "", depth: int = 3) -> list[str]:
    """Dotted paths where two JSON values differ, objects descended up to depth levels."""
    if left == right:
        return []
    if depth == 0 or not isinstance(left, dict) or not isinstance(right, dict):
        return [prefix.rstrip(".")] if prefix else []
    paths = []
    for key in sorted(set(left) | set(right), key=str):
        paths += changed_paths(left.get(key, _MISSING), right.get(key, _MISSING), f"{prefix}{key}.", depth - 1)
    return paths


def diff_cases(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Name the added, removed and changed cases, with the fields that changed in each."""
    changed = {
        name: changed_paths(before[name], after[name])
        for name in sorted(set(before) & set(after))
        if before[name] != after[name]
    }
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)), "changed": changed}


def golden_diffs(base: str) -> dict[str, dict[str, Any]]:
    diffs = {}
    for path in GOLDEN_PATHS:
        current = CONFORMANCE_DIR / path
        after = json.loads(current.read_text(encoding="utf-8")) if current.exists() else None
        diffs[path] = diff_cases(golden_cases(path, git_show(base, path)), golden_cases(path, after))
    return diffs


def failure_lines(log: Path, limit: int = 40) -> list[str]:
    """The lines of a suite log that name a failure."""
    if not log.exists():
        return []
    lines = [
        line.strip()
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
        if re.search(r"\bFAIL|✗|\bfailed\b", line) and not re.search(r"\b0 failed\b", line)
    ]
    return lines[:limit] + ([f"… {len(lines) - limit} more"] if len(lines) > limit else [])


def render_summary(
    changes: list[dict[str, Any]],
    diffs: dict[str, dict[str, Any]],
    suites: list[tuple[str, int | None, Path | None]],
    run_url: str | None = None,
) -> str:
    """The pull request body: version changes, golden case changes and suite results."""
    lines = [
        "## Motivation",
        "",
        "Keeps the conformance suite on each SDK's latest release, so mppx (the reference) and the "
        "other SDKs are tested as users install them. The flow goldens and pair snapshots are "
        "regenerated from the new mppx: every case listed under them changed in mppx's wire "
        "behavior and needs a review before merging.",
        "",
        "## Summary",
        "",
        "| Adapter | Package | From | To |",
        "| --- | --- | --- | --- |",
    ]
    for record in changes:
        if "error" in record:
            to = f"not bumped: {record['error'].splitlines()[0][:200]}"
        else:
            to = record["to"] if record.get("changed") else f"{record['to']} (unchanged)"
        package = f"`{record['package']}` ({record['registry']})"
        lines.append(f"| {record['adapter']} | {package} | {record.get('from') or '?'} | {to} |")
    lines += ["", "### Golden and snapshot changes", ""]
    for path, diff in diffs.items():
        total = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
        if not total:
            lines.append(f"- `{path}`: no changes.")
            continue
        lines.append(f"- `{path}` ({GOLDEN_PATHS.get(path, path)}): {total} case(s)")
        for name, fields in diff["changed"].items():
            named = ", ".join(f"`{field}`" for field in fields)
            lines.append(f"  - changed `{name}`" + (f": {named}" if named else ""))
        lines += [f"  - added `{name}`" for name in diff["added"]]
        lines += [f"  - removed `{name}`" for name in diff["removed"]]
    lines += ["", "## Testing", "", "| Suite | Result |", "| --- | --- |"]
    details: list[str] = []
    for name, status, log in suites:
        result = "not run" if status is None else ("passed" if status == 0 else "failed")
        lines.append(f"| {name} | {result} |")
        failed = failure_lines(log) if log is not None and status not in (None, 0) else []
        if failed:
            details += ["", f"{name} failures:", "", "```", *failed, "```"]
    lines += details
    if run_url:
        lines += ["", f"Generated by [Bump reference SDK pins]({run_url})."]
    body = "\n".join(lines) + "\n"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n… truncated; see the workflow run for the rest.\n"
    return body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_suite(value: str) -> tuple[str, int | None, Path | None]:
    """NAME=STATUS[:LOG], for example vectors=1:vectors.log; STATUS skipped means not run."""
    name, _, rest = value.partition("=")
    status, _, log = rest.partition(":")
    if not name or not status:
        raise argparse.ArgumentTypeError(f"expected NAME=STATUS[:LOG], got {value!r}")
    try:
        code = None if status == "skipped" else int(status)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an exit status or skipped, got {status!r}") from error
    return name, code, (Path(log) if log else None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bump the reference SDK pins to each SDK's latest release.")
    commands = parser.add_subparsers(dest="command", required=True)
    bump_parser = commands.add_parser("bump", help="Pin every adapter's SDK to its latest release")
    bump_parser.add_argument("--adapters", default=",".join(SDKS), help="Comma-separated adapters (default: all)")
    bump_parser.add_argument("--changes", type=Path, required=True, help="Write the version changes here (JSON)")
    summary_parser = commands.add_parser("summary", help="Write the pull request body")
    summary_parser.add_argument("--changes", type=Path, required=True, help="The bump step's JSON")
    summary_parser.add_argument("--base", default="HEAD", help="Commit to diff the goldens against (default: HEAD)")
    summary_parser.add_argument("--suite", type=parse_suite, action="append", default=[], help="NAME=STATUS[:LOG]")
    summary_parser.add_argument("--output", type=Path, help="Write the body here instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "bump":
        adapters = [name for name in args.adapters.split(",") if name]
        unknown = sorted(set(adapters) - set(SDKS))
        if unknown:
            print(f"Unknown adapters: {', '.join(unknown)}", file=sys.stderr)
            return 2
        records = bump(adapters)
        args.changes.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        return 1 if any("error" in record for record in records) else 0
    changes = json.loads(args.changes.read_text(encoding="utf-8"))
    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        server, repository = os.environ.get("GITHUB_SERVER_URL", "https://github.com"), os.environ.get("GITHUB_REPOSITORY", "")
        run_url = f"{server}/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body = render_summary(changes, golden_diffs(args.base), args.suite, run_url)
    if args.output:
        args.output.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
