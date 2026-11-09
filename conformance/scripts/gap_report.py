#!/usr/bin/env python3
"""Report which BCP 14 requirements in the MPP specifications conformance covers.

Reads every `specs/**/*.md` draft in a tempoxyz/mpp-specs checkout (a fresh
clone of main when --specs is omitted) and extracts each paragraph or list item
that contains an RFC 2119/8174 keyword in capitals. A requirement's key is the
`{: #req-<draft>-<nnn>}` IAL right after it when present, otherwise
`<draft>#<section>:<sha256 prefix>`. Vector scenarios and flow cases claim
requirement IDs with an optional `requirements` array; adapter manifests claim
drafts with an optional `profile`. The report lists, per draft, how many rules
there are, how many carry an ID and how many a vector or flow covers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness import (
    ADAPTERS_DIR,
    AdapterConfig,
    COMMAND_TO_OPERATION,
    CONFORMANCE_DIR,
    FLOW_CASES_PATH,
    VECTORS_DIR,
    load_flow_cases,
    load_json,
    load_vector,
    validate_value,
)
from vector_runner import VectorRunner

SPECS_REPOSITORY = "https://github.com/tempoxyz/mpp-specs"
EXCERPT_LENGTH = 160

# Longest alternatives first so "MUST NOT" is not read as "MUST".
KEYWORD_RE = re.compile(
    r"(?<![A-Za-z\"'])(?:MUST NOT|SHALL NOT|SHOULD NOT|NOT RECOMMENDED|MUST|REQUIRED|SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)(?![A-Za-z\"'])"
)
REQ_ID_RE = re.compile(r"^req-[a-z0-9-]+-[0-9]{3}$")
IAL_LINE_RE = re.compile(r"^\s*\{:(?P<body>[^}]*)\}\s*$")
# A trailing IAL: '{:' then no '{' or '}' up to the closing '}' at the end. Anchored on
# its own '{:' (no leading whitespace quantifier) so a search stays linear.
INLINE_IAL_RE = re.compile(r"\{:(?P<body>[^{}]*)\}\s*$")
IAL_ID_RE = re.compile(r"#(req-[A-Za-z0-9_-]+)")
# Headings are parsed in two linear steps (see parse_heading) rather than with one regex
# whose lazy title and optional whitespace runs backtrack on long whitespace.
HEADING_RE = re.compile(r"^#{1,6}\s+(?P<rest>.*)$")
HEADING_ANCHOR_RE = re.compile(r"\{#(?P<anchor>[^{}\s]+)\}$")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
DEFINITION_RE = re.compile(r"^\s*:\s+")
FENCE_RE = re.compile(r"^\s*(\x60{3,}|~{3,})")


def parse_heading(line: str) -> tuple[str, str | None] | None:
    """Return (title, anchor) for a Markdown heading line, or None."""
    match = HEADING_RE.match(line)
    if not match:
        return None
    rest = match.group("rest").strip()
    anchor = HEADING_ANCHOR_RE.search(rest)
    if anchor:
        return rest[: anchor.start()].rstrip(), anchor.group("anchor")
    return rest, None


@dataclass
class Requirement:
    """One BCP 14 rule: a paragraph or list item with a keyword in capitals."""

    draft: str
    file: str
    line: int
    section: str
    text: str
    req_id: str | None = None
    fallback_key: str = ""
    keywords: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.req_id or self.fallback_key


def draft_short_name(docname: str) -> str:
    """Map a draft name to the short name requirement IDs use, such as tempo-charge."""
    name = re.sub(r"-\d{2}$", "", docname.removesuffix(".md"))
    name = name.removeprefix("draft-")
    if name == "httpauth-payment":
        return "core"
    return name.removeprefix("payment-")


def slugify(text: str) -> str:
    """Slug a heading the way kramdown-rfc derives an anchor from it."""
    text = re.sub(r"[\x60*_]", "", text).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "section"


def normalize(text: str) -> str:
    """Collapse whitespace and drop list markers so reflowing keeps the hash."""
    text = LIST_ITEM_RE.sub("", text, count=1)
    text = DEFINITION_RE.sub("", text, count=1)
    return " ".join(text.split())


def keywords_in(text: str) -> list[str]:
    """Return the BCP 14 keywords in text, ignoring quoted ones such as "MUST"."""
    unquoted = re.sub(r"\x60[^\x60]*\x60", "", text)
    return sorted(set(KEYWORD_RE.findall(unquoted)))


def ial_req_id(body: str) -> str | None:
    match = IAL_ID_RE.search(body)
    return match.group(1) if match else None


def extract_requirements(
    path: Path, relative: str
) -> tuple[list[Requirement], list[str], int]:
    """Extract every requirement in one draft, with warnings and the keyword table rows.

    Table rows (field tables with REQUIRED or OPTIONAL columns) cannot carry an
    IAL each, so they are counted rather than extracted; schemas cover them.
    """
    draft = draft_short_name(path.name)
    lines = path.read_text(encoding="utf-8").splitlines()
    requirements: list[Requirement] = []
    warnings: list[str] = []
    section = "abstract"
    unit: list[str] = []
    unit_line = 0
    fence: str | None = None
    in_comment = False
    table_rows = 0

    def flush(req_id: str | None = None) -> None:
        nonlocal unit
        if not unit:
            return
        text = "\n".join(unit)
        unit = []
        inline = INLINE_IAL_RE.search(text)
        if inline:
            req_id = req_id or ial_req_id(inline.group("body"))
            text = text[: inline.start()].rstrip()
        found = keywords_in(text)
        if not found:
            if req_id:
                warnings.append(
                    f"{relative}:{unit_line}: {req_id} tags text without a BCP 14 keyword"
                )
            return
        normalized = normalize(text)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        if req_id and not REQ_ID_RE.match(req_id):
            warnings.append(
                f"{relative}:{unit_line}: {req_id} does not match req-<draft>-<nnn>"
            )
        requirements.append(
            Requirement(
                draft=draft,
                file=relative,
                line=unit_line,
                section=section,
                text=normalized,
                req_id=req_id,
                fallback_key=f"{draft}#{section}:{digest}",
                keywords=found,
            )
        )

    start = 0
    if lines and lines[0].strip() == "---":
        # kramdown-rfc front matter ends at a bare --- or at the first "--- abstract"-style marker.
        for index in range(1, len(lines)):
            closing = lines[index].strip()
            if closing == "---":
                start = index + 1
                break
            if closing.startswith("--- "):
                start = index
                break

    for number, line in enumerate(lines[start:], start=start + 1):
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        fence_match = FENCE_RE.match(line)
        if fence_match:
            flush()
            fence = fence_match.group(1)[0] * 3
            continue
        if stripped.startswith("<!--"):
            flush()
            in_comment = "-->" not in stripped
            continue
        ial = IAL_LINE_RE.match(line)
        if ial:
            req_id = ial_req_id(ial.group("body"))
            if req_id and not unit:
                warnings.append(
                    f"{relative}:{number}: {req_id} is not on the line right after a rule, so it tags nothing"
                )
            flush(req_id)
            continue
        if stripped.startswith("|"):
            flush()
            table_rows += 1 if keywords_in(stripped) else 0
            continue
        if not stripped or stripped.startswith("--- "):
            flush()
            continue
        heading = parse_heading(line)
        if heading:
            flush()
            section = heading[1] or slugify(heading[0])
            continue
        if LIST_ITEM_RE.match(line) or DEFINITION_RE.match(line):
            flush()
        if not unit:
            unit_line = number
        unit.append(
            stripped.removeprefix("> ").removeprefix(">")
            if stripped.startswith(">")
            else stripped
        )
    flush()

    seen: dict[str, int] = {}
    for requirement in requirements:
        count = seen.get(requirement.fallback_key, 0) + 1
        seen[requirement.fallback_key] = count
        if count > 1:
            requirement.fallback_key = f"{requirement.fallback_key}-{count}"
        if requirement.req_id and not requirement.req_id.startswith(f"req-{draft}-"):
            warnings.append(
                f"{relative}:{requirement.line}: {requirement.req_id} does not use this draft's prefix req-{draft}-"
            )
    return requirements, warnings, table_rows


def draft_paths(specs_dir: Path) -> list[Path]:
    """Return the drafts under specs/, or fail when specs_dir is not an mpp-specs checkout."""
    if not specs_dir.is_dir():
        raise ValueError(f"specs path {specs_dir} does not exist or is not a directory")
    paths = sorted((specs_dir / "specs").rglob("*.md"))
    if not paths:
        raise ValueError(
            f"{specs_dir} has no specs/**/*.md drafts; pass an mpp-specs checkout"
        )
    return paths


def extract_all(specs_dir: Path) -> tuple[list[Requirement], list[str], dict[str, int]]:
    """Extract requirements from every draft under specs/, flagging duplicate IDs."""
    requirements: list[Requirement] = []
    warnings: list[str] = []
    table_rows: dict[str, int] = {}
    first_path: dict[str, str] = {}
    # Highest revision first, so a stray older copy of a draft is the one skipped.
    for path in sorted(draft_paths(specs_dir), key=lambda p: p.name, reverse=True):
        relative = path.relative_to(specs_dir).as_posix()
        short = draft_short_name(path.name)
        if short in first_path:
            # Two files with one short name would merge their rules into one draft.
            warnings.append(
                f"{relative}: same draft name '{short}' as {first_path[short]}; skipped"
            )
            continue
        first_path[short] = relative
        found, found_warnings, rows = extract_requirements(path, relative)
        requirements.extend(found)
        warnings.extend(found_warnings)
        table_rows[draft_short_name(path.name)] = rows
    owners: dict[str, Requirement] = {}
    for requirement in requirements:
        if not requirement.req_id:
            continue
        other = owners.get(requirement.req_id)
        if other:
            warnings.append(
                f"{requirement.file}:{requirement.line}: {requirement.req_id} already used at {other.file}:{other.line}"
            )
        else:
            owners[requirement.req_id] = requirement
    return requirements, warnings, table_rows


@dataclass
class Claim:
    """A vector scenario or flow case and the requirement IDs it covers."""

    source: str
    requirements: list[str]
    operation: str | None = None
    adapters: list[str] | None = None
    sdk_versions: dict[str, str] | None = None
    # Flow cases only: server_only, raw_http, client_flow or ordinary, as flow_runner runs them.
    flow_mode: str | None = None


def vector_operation(vector: dict[str, Any]) -> str | None:
    commands = vector["commands"]
    if "operation" in commands:
        return commands["operation"]
    for command in ("parse", "format", "generate"):
        if command in commands:
            return COMMAND_TO_OPERATION.get(commands[command])
    return None


def load_claims(
    vectors_dir: Path = VECTORS_DIR, flow_cases_path: Path = FLOW_CASES_PATH
) -> list[Claim]:
    """Read the requirement IDs each vector scenario and flow case claims."""
    claims: list[Claim] = []
    for path in sorted(vectors_dir.glob("*.json")):
        if path.name == "package.json":
            continue
        vector = load_vector(path)
        operation = vector_operation(vector)
        for scenario in vector["scenarios"]:
            if scenario.get("requirements"):
                claims.append(
                    Claim(
                        source=f"vectors/{path.name}#{scenario['name']}",
                        requirements=scenario["requirements"],
                        operation=operation,
                        adapters=scenario.get("adapters"),
                        sdk_versions=scenario.get("sdkVersions"),
                    )
                )
    for flow_case in load_flow_cases(flow_cases_path):
        if flow_case.get("requirements"):
            claims.append(
                Claim(
                    source=f"flows/{flow_cases_path.name}#{flow_case['name']}",
                    requirements=flow_case["requirements"],
                    flow_mode=flow_mode(flow_case),
                )
            )
    return claims


def flow_mode(flow_case: dict[str, Any]) -> str:
    """Classify a flow case the way flow_runner dispatches it."""
    if flow_case.get("server_only"):
        return "server_only"
    # Same order as flow_runner's dispatch: discovery and JSON-RPC cases send raw
    # HTTP without the adapter; client_flow wins over no_payment; a remaining
    # no_payment case also never calls the adapter.
    if flow_case.get("discovery") or flow_case.get("json_rpc"):
        return "raw_http"
    if flow_case.get("client_flow"):
        return "client_flow"
    if flow_case.get("no_payment"):
        return "raw_http"
    return "ordinary"


def load_profiles(adapters_dir: Path = ADAPTERS_DIR) -> dict[str, dict[str, Any]]:
    """Read each adapter manifest's name, capabilities and optional profile."""
    profiles: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(adapters_dir.glob("*/adapter.json")):
        manifest = load_json(manifest_path)
        validate_value(manifest, "adapter-manifest.schema.json", str(manifest_path))
        if manifest.get("profile"):
            profiles[manifest["name"]] = {
                "profile": manifest["profile"],
                "capabilities": manifest["capabilities"],
            }
    return profiles


VersionCheck = Callable[[Claim, str], bool]


def claim_applies(
    claim: Claim,
    adapter: str,
    capabilities: list[str],
    version_applies: VersionCheck | None = None,
) -> bool:
    """Whether the runners would run this claim against the adapter."""
    if claim.flow_mode is not None:
        # server_only and raw_http cases never call the adapter; only client_flow
        # cases drive the adapter's http.payment_request.
        if claim.flow_mode in ("server_only", "raw_http"):
            return False
        return (
            claim.flow_mode != "client_flow" or "http.payment_request" in capabilities
        )
    if claim.adapters is not None and adapter not in claim.adapters:
        return False
    if claim.operation is not None and claim.operation not in capabilities:
        return False
    return version_applies is None or version_applies(claim, adapter)


def version_checker(
    warnings: list[str], resolver: Callable[[str], str] | None = None
) -> VersionCheck:
    """Evaluate a claim's sdkVersions with the vector runner's own version logic.

    An adapter whose pinned SDK version cannot be resolved (its toolchain is
    missing) counts none of its version-constrained claims, with a warning.
    """
    runner = VectorRunner(sdk_version_resolver=resolver)
    unresolved: set[str] = set()

    def applies(claim: Claim, adapter: str) -> bool:
        if not claim.sdk_versions or adapter not in claim.sdk_versions:
            return True
        config = AdapterConfig(name=adapter, command=[], capabilities=[])
        try:
            matched, _ = runner.scenario_version_applies(
                {"sdkVersions": claim.sdk_versions}, config
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            RuntimeError,
            StopIteration,
            KeyError,
        ):
            if adapter not in unresolved:
                unresolved.add(adapter)
                warnings.append(
                    f"{adapter}: pinned SDK version unknown; its sdkVersions-constrained claims are not counted"
                )
            return False
        return matched

    return applies


def excerpt(text: str) -> str:
    return (
        text
        if len(text) <= EXCERPT_LENGTH
        else text[: EXCERPT_LENGTH - 1].rstrip() + "…"
    )


def build_report(
    requirements: list[Requirement],
    claims: list[Claim],
    profiles: dict[str, dict[str, Any]],
    warnings: list[str],
    specs: dict[str, str | None],
    table_rows: dict[str, int] | None = None,
    version_applies: VersionCheck | None = None,
) -> dict[str, Any]:
    """Assemble the JSON report: per-draft counts, uncovered rules and profile coverage."""
    coverage: dict[str, list[str]] = {}
    for claim in claims:
        for req_id in claim.requirements:
            coverage.setdefault(req_id, []).append(claim.source)
    known = {requirement.req_id for requirement in requirements if requirement.req_id}
    unknown = sorted(req_id for req_id in coverage if req_id not in known)

    drafts: dict[str, dict[str, Any]] = {}
    for requirement in requirements:
        entry = drafts.setdefault(
            requirement.draft,
            {
                "draft": requirement.draft,
                "file": requirement.file,
                "rules": 0,
                "tagged": 0,
                "covered": 0,
                "table_rows": (table_rows or {}).get(requirement.draft, 0),
                "uncovered": [],
            },
        )
        entry["rules"] += 1
        if requirement.req_id:
            entry["tagged"] += 1
        if requirement.req_id in coverage:
            entry["covered"] += 1
        else:
            entry["uncovered"].append(
                {
                    "key": requirement.key,
                    "line": requirement.line,
                    "section": requirement.section,
                    "keywords": requirement.keywords,
                    "excerpt": excerpt(requirement.text),
                }
            )

    profile_report: dict[str, Any] = {}
    for adapter, manifest in sorted(profiles.items()):
        applicable = {
            req_id
            for claim in claims
            if claim_applies(claim, adapter, manifest["capabilities"], version_applies)
            for req_id in claim.requirements
        }
        per_draft: dict[str, dict[str, int]] = {}
        for draft in manifest["profile"]:
            rules = [
                requirement
                for requirement in requirements
                if requirement.draft == draft
            ]
            per_draft[draft] = {
                "rules": len(rules),
                "covered": sum(
                    1 for requirement in rules if requirement.req_id in applicable
                ),
            }
        profile_report[adapter] = {
            "profile": manifest["profile"],
            "drafts": per_draft,
            "unknown_drafts": sorted(
                draft for draft in manifest["profile"] if draft not in drafts
            ),
        }

    totals = {
        "rules": len(requirements),
        "tagged": sum(entry["tagged"] for entry in drafts.values()),
        "covered": sum(entry["covered"] for entry in drafts.values()),
        "table_rows": sum(entry["table_rows"] for entry in drafts.values()),
    }
    return {
        "specs": specs,
        "totals": totals,
        "drafts": sorted(drafts.values(), key=lambda entry: entry["file"]),
        "coverage": {
            req_id: sorted(sources) for req_id, sources in sorted(coverage.items())
        },
        "unknown_references": unknown,
        "profiles": profile_report,
        "warnings": warnings,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report as a Markdown summary with the uncovered rules per draft."""
    specs = report["specs"]
    totals = report["totals"]
    lines = [
        "# MPP requirement gap report",
        "",
        f"Specs: {specs.get('source')} at {specs.get('commit') or 'unknown commit'}.",
        "",
        f"{totals['rules']} rules, {totals['tagged']} tagged with an ID, {totals['covered']} covered by a vector or flow.",
        f"{totals['table_rows']} table rows also carry a keyword; they are counted, not extracted.",
        "",
        "| Draft | Rules | Tagged | Covered | Table rows |",
        "|-------|------:|-------:|--------:|-----------:|",
    ]
    for entry in report["drafts"]:
        lines.append(
            f"| {entry['draft']} | {entry['rules']} | {entry['tagged']} | {entry['covered']} | {entry['table_rows']} |"
        )
    if report["profiles"]:
        lines += [
            "",
            "## SDK profiles",
            "",
            "| Adapter | Draft | Rules | Covered |",
            "|---------|-------|------:|--------:|",
        ]
        for adapter, entry in report["profiles"].items():
            for draft, counts in entry["drafts"].items():
                lines.append(
                    f"| {adapter} | {draft} | {counts['rules']} | {counts['covered']} |"
                )
    if report["unknown_references"]:
        lines += ["", "## Unknown requirement IDs", ""]
        lines += [
            f"- \x60{req_id}\x60: {', '.join(report['coverage'][req_id])}"
            for req_id in report["unknown_references"]
        ]
    if report["warnings"]:
        lines += ["", "## Warnings", ""]
        lines += [f"- {warning}" for warning in report["warnings"]]
    lines += ["", "## Uncovered rules"]
    for entry in report["drafts"]:
        if not entry["uncovered"]:
            continue
        lines += ["", f"### {entry['draft']} ({entry['file']})", ""]
        for rule in entry["uncovered"]:
            text = rule["excerpt"].replace("|", "\\|")
            lines.append(f"- \x60{rule['key']}\x60 (line {rule['line']}): {text}")
    return "\n".join(lines) + "\n"


def git_commit(path: Path) -> str | None:
    """Return the commit the specs at path were read from, or None if unknown.

    None when path is not the root of a git checkout (its HEAD would belong to an
    enclosing repository); a "-dirty" suffix when specs/ has uncommitted changes
    or untracked files, since draft discovery reads every specs/**/*.md in the
    working tree and the counted rules then differ from that commit's tree.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        if Path(git("rev-parse", "--show-toplevel")).resolve() != path.resolve():
            return None
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=all", "--", "specs"))
    except (OSError, subprocess.CalledProcessError):
        return None
    if not commit:
        return None
    return f"{commit}-dirty" if dirty else commit


def clone_specs(destination: Path, ref: str) -> Path:
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            ref,
            SPECS_REPOSITORY,
            str(destination),
        ],
        check=True,
    )
    return destination


def generate(specs_dir: Path, source: str) -> dict[str, Any]:
    """Build the report for one mpp-specs checkout against this suite's fixtures."""
    requirements, warnings, table_rows = extract_all(specs_dir)
    claims = load_claims()
    profiles = load_profiles()
    specs: dict[str, str | None] = {"source": source, "commit": git_commit(specs_dir)}
    return build_report(
        requirements,
        claims,
        profiles,
        warnings,
        specs,
        table_rows,
        version_checker(warnings),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--specs",
        type=Path,
        help="mpp-specs checkout; clones tempoxyz/mpp-specs when omitted",
    )
    parser.add_argument(
        "--ref", default="main", help="branch or tag to clone when --specs is omitted"
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=CONFORMANCE_DIR / "gap-report.json",
        help="JSON report path",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=CONFORMANCE_DIR / "gap-report.md",
        help="Markdown summary path",
    )
    args = parser.parse_args(argv)

    try:
        if args.specs:
            report = generate(args.specs.resolve(), str(args.specs))
        else:
            with tempfile.TemporaryDirectory(prefix="mpp-specs-") as directory:
                specs_dir = clone_specs(Path(directory) / "mpp-specs", args.ref)
                report = generate(specs_dir, f"{SPECS_REPOSITORY}@{args.ref}")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    totals = report["totals"]
    print(
        f"{totals['rules']} rules, {totals['tagged']} tagged, {totals['covered']} covered; "
        f"wrote {args.json} and {args.markdown}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
