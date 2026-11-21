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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
# The secure runner's registry proxy refuses packages published in about the
# last day ("recentlyPublished"), so a release that new could not install in CI.
DEFAULT_MIN_AGE_DAYS = 1
# Releases published after this are skipped; bump() sets it.
cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_MIN_AGE_DAYS)


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_json(url: str) -> Any:
    return json.loads(fetch(url))


Release = tuple[str, datetime]


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_stable(version: str) -> bool:
    return re.fullmatch(r"v?\d+(\.\d+)*", version) is not None


def pick_release(releases: list[Release], cutoff: datetime) -> tuple[str, str]:
    """Return (chosen, newest): the newest stable release published before cutoff, and the newest overall."""
    stable = sorted((r for r in releases if _is_stable(r[0])), key=lambda r: _version_tuple(r[0]))
    if not stable:
        raise RuntimeError("no stable release")
    old_enough = [version for version, published in stable if published <= cutoff]
    if not old_enough:
        raise RuntimeError(f"no stable release published before {cutoff:%Y-%m-%d %H:%M} UTC")
    return old_enough[-1], stable[-1][0]


def npm_releases(package: str) -> list[Release]:
    document = fetch_json(f"https://registry.npmjs.org/{package}")
    latest = _version_tuple(document["dist-tags"]["latest"])
    times = document.get("time", {})
    # Never above the latest dist-tag, which npm install would pick.
    return [
        (version, _parse_time(times[version]))
        for version in document.get("versions", {})
        if version in times and _version_tuple(version) <= latest
    ]


def pypi_releases(package: str) -> list[Release]:
    releases = fetch_json(f"https://pypi.org/pypi/{package}/json")["releases"]
    return [
        (version, min(_parse_time(item["upload_time_iso_8601"]) for item in files))
        for version, files in releases.items()
        if files and not all(item.get("yanked") for item in files)
    ]


def crates_releases(package: str) -> list[Release]:
    versions = fetch_json(f"https://crates.io/api/v1/crates/{package}/versions")["versions"]
    return [(item["num"], _parse_time(item["created_at"])) for item in versions if not item.get("yanked")]


def go_releases(module: str, newest: int = 5) -> list[Release]:
    base = f"https://proxy.golang.org/{module.lower()}/@v"
    versions = [line for line in fetch(f"{base}/list").decode().split() if _is_stable(line)]
    versions = sorted(versions, key=_version_tuple)[-newest:]
    return [(version, _parse_time(fetch_json(f"{base}/{version}.info")["Time"])) for version in versions]


def rubygems_releases(gem: str) -> list[Release]:
    versions = fetch_json(f"https://rubygems.org/api/v1/versions/{gem}.json")
    return [(item["number"], _parse_time(item["created_at"])) for item in versions if not item.get("prerelease")]


def maven_releases(group: str, artifact: str, newest: int = 5) -> list[Release]:
    base = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}"
    root = ElementTree.fromstring(fetch(f"{base}/maven-metadata.xml"))
    versions = sorted(
        (item.text or "" for item in root.iterfind("versioning/versions/version") if _is_stable(item.text or "")),
        key=_version_tuple,
    )[-newest:]
    releases = []
    for version in versions:
        request = urllib.request.Request(f"{base}/{version}/{artifact}-{version}.pom", method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            releases.append((version, parsedate_to_datetime(response.headers["Last-Modified"])))
    return releases


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
    releases: Callable[[], list[Release]]
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
        packages.append(f"viem@{pick_release(npm_releases('viem'), cutoff)[0]}")
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
    # Conservative: move mpp-rb only, not its dependencies.
    run(["bundle", "lock", "--update", "mpp-rb", "--conservative"], ADAPTERS_DIR / "ruby")


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
        Sdk("typescript", "mppx", "npm", lambda: npm_releases("mppx"), _typescript_current, _no_manifest_edit, _typescript_lock),
        Sdk(
            "python", "pympp", "PyPI", lambda: pypi_releases("pympp"),
            lambda: toml_package_version(ADAPTERS_DIR / "python" / "uv.lock", "pympp"), _python_pin, _python_lock,
        ),
        Sdk(
            "rust", "mpp", "crates.io", lambda: crates_releases("mpp"),
            lambda: toml_package_version(ADAPTERS_DIR / "rust" / "Cargo.lock", "mpp"), _rust_pin, _rust_lock,
        ),
        Sdk(
            "go", "github.com/tempoxyz/mpp-go", "Go module proxy", lambda: go_releases("github.com/tempoxyz/mpp-go"),
            lambda: lock_version(ADAPTERS_DIR / "go" / "go.mod", r"^\s*github\.com/tempoxyz/mpp-go\s+(\S+)"),
            _no_manifest_edit, _go_lock,
        ),
        Sdk(
            "ruby", "mpp-rb", "RubyGems", lambda: rubygems_releases("mpp-rb"),
            lambda: lock_version(ADAPTERS_DIR / "ruby" / "Gemfile.lock", r"^    mpp-rb \(([^)]+)\)"), _ruby_pin, _ruby_lock,
        ),
        Sdk(
            "java", "com.stripe:mpp-java", "Maven Central", lambda: maven_releases("com.stripe", "mpp-java"),
            lambda: lock_version(ADAPTERS_DIR / "java" / "gradle.lockfile", r"^com\.stripe:mpp-java:([^=]+)="),
            _java_pin, _java_lock,
        ),
    )
}


def bump(adapters: list[str], min_age_days: float = DEFAULT_MIN_AGE_DAYS, now: datetime | None = None) -> list[dict[str, Any]]:
    """Pin each adapter's SDK to its newest release at least min_age_days old; return one record per adapter."""
    global cutoff
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=min_age_days)
    records = []
    for name in adapters:
        sdk = SDKS[name]
        record: dict[str, Any] = {"adapter": name, "package": sdk.package, "registry": sdk.registry}
        try:
            record["from"] = sdk.current()
            record["to"], newest = pick_release(sdk.releases(), cutoff)
            # Never move a pin back, as when it already holds a release newer than the cutoff.
            if record["from"] and _is_stable(record["from"]) and _version_tuple(record["from"]) > _version_tuple(record["to"]):
                record["to"] = record["from"]
            if newest != record["to"]:
                record["newest"] = newest
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
        # Numbered failure entries name their adapter; totals and failed pairings follow them.
        if re.search(r"^\s*\d+\) \S|^\s*FAILED:|: failed\s*$|✗", line)
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
            if record.get("newest"):
                to += f" ({record['newest']} is too new for the CI registry proxy)"
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
    bump_parser.add_argument(
        "--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS,
        help=f"Skip releases newer than this; CI's registry proxy refuses them (default: {DEFAULT_MIN_AGE_DAYS})",
    )
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
        records = bump(adapters, args.min_age_days)
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
