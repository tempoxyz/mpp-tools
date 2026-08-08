from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import PurePosixPath

from .models import (
    Automation,
    CanonicalChange,
    Decision,
    DecisionKind,
    LabelResolution,
    Manifest,
    PropagationOutcome,
    PropagationResult,
    PullRequestFile,
)

_PROPAGATION_TABLE_START = "<!-- agricola:propagation-table:start -->"
_PROPAGATION_TABLE_END = "<!-- agricola:propagation-table:end -->"


class FileCategory(StrEnum):
    NORMATIVE = "normative"
    CANONICAL = "canonical"
    INCIDENTAL = "incidental"


_NORMATIVE_DIRECTORIES = {
    "conformance",
    "protocol",
    "protocols",
    "rfc",
    "rfcs",
    "spec",
    "specs",
    "vector",
    "vectors",
}
_NORMATIVE_FILES = {"spec.md", "specification.md"}
_INCIDENTAL_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "eslint.config.js",
    "eslint.config.mjs",
}
_INCIDENTAL_PREFIXES = (".github/", ".changeset/", "docs/site/", "scripts/release/")


def classify_file(file: PullRequestFile) -> FileCategory:
    path = file.path.lower()
    parts = PurePosixPath(path).parts
    basename = parts[-1]
    if set(parts[:-1]) & _NORMATIVE_DIRECTORIES or basename in _NORMATIVE_FILES:
        return FileCategory.NORMATIVE
    if (
        basename in _INCIDENTAL_NAMES
        or path == "scripts/release"
        or path.startswith(_INCIDENTAL_PREFIXES)
    ):
        return FileCategory.INCIDENTAL
    return FileCategory.CANONICAL


def _file_line(file: PullRequestFile) -> str:
    change = f"+{file.additions}/-{file.deletions}"
    return f"- `{file.path}` ({file.status}, {change})"


def _section(title: str, files: Iterable[PullRequestFile], empty: str) -> list[str]:
    selected = list(files)
    lines = [f"### {title}", ""]
    lines.extend((_file_line(file) for file in selected) if selected else [empty])
    lines.append("")
    return lines


def _pull_request_link(reference: str) -> str:
    repo, number = reference.rsplit("#", 1)
    return f"[{reference}](https://github.com/{repo}/pull/{number})"


def _table_row(target: str, mode: str, status: str, pull_request: str = "—") -> str:
    return f"| `{target}` | {mode} | {status} | {pull_request} |"


def _propagation_table(
    manifest: Manifest,
    selected_targets: Iterable[str],
    decisions: Sequence[Decision],
    *,
    targets: Iterable[str] | None = None,
) -> list[str]:
    selected = set(selected_targets)
    visible = set(targets) if targets is not None else None
    by_target = {decision.target: decision for decision in decisions}
    rows = [
        _PROPAGATION_TABLE_START,
        "| Target | Automation | Status | Pull request |",
        "| --- | --- | --- | --- |",
    ]
    for target, sdk in manifest.sdks.items():
        if visible is not None and target not in visible:
            continue
        decision = by_target.get(target)
        if decision is not None and decision.decision is DecisionKind.PROPAGATE:
            assert decision.pr is not None
            status = "Recorded"
            pull_request = _pull_request_link(decision.pr)
        elif decision is not None:
            reason = decision.reason.replace("|", "\\|") if decision.reason else ""
            status = f"Skipped — {reason}"
            pull_request = "—"
        elif target in selected:
            status = "Queued"
            pull_request = "—"
        elif sdk.automation is Automation.NOTIFY:
            status = "Notification only"
            pull_request = "—"
        else:
            status = "Awaiting decision"
            pull_request = "—"
        rows.append(_table_row(target, sdk.automation.value, status, pull_request))
    rows.append(_PROPAGATION_TABLE_END)
    return rows


def propagation_table(
    manifest: Manifest,
    targets: Iterable[str],
    *,
    queued_targets: Iterable[str] = (),
) -> list[str]:
    """Render propagation state for a selected set of SDKs."""
    selected = tuple(targets)
    return _propagation_table(
        manifest,
        queued_targets,
        (),
        targets=selected,
    )


def quick_fix_lines() -> list[str]:
    return [
        "### Quick action",
        "",
        "Use GitHub's copy button, then post this command as a comment:",
        "",
        "```text",
        "/ag fix",
        "```",
    ]


def queued_propagations(body: str, targets: Iterable[str]) -> str:
    replacements = {target: _table_row(target, "pr", "Queued") for target in targets}
    return _replace_propagation_rows(body, replacements)


def recorded_propagations(body: str) -> dict[str, str]:
    recorded: dict[str, str] = {}
    pattern = re.compile(
        r"^\| `(?P<target>[a-z][a-z0-9-]*)` \| pr \| Recorded \| "
        r"\[(?P<reference>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*)\]"
    )
    for line in body.splitlines():
        if match := pattern.match(line):
            recorded[match.group("target")] = match.group("reference")
    return recorded


def preserve_propagation_state(body: str, previous_body: str) -> str:
    previous_rows = {
        line.split(" |", 1)[0]: line
        for line in previous_body.splitlines()
        if line.startswith("| `")
        and any(
            status in line for status in ("| Queued |", "| Recorded |", "| Skipped —")
        )
    }
    replacements = {}
    for line in body.splitlines():
        if line.startswith("| `"):
            key = line.split(" |", 1)[0]
            if key in previous_rows:
                target = key.removeprefix("| `").removesuffix("`")
                replacements[target] = previous_rows[key]
    return _replace_propagation_rows(body, replacements)


def _replace_propagation_rows(body: str, replacements: dict[str, str]) -> str:
    lines = body.splitlines()
    if _PROPAGATION_TABLE_START not in lines or _PROPAGATION_TABLE_END not in lines:
        return body
    start = lines.index(_PROPAGATION_TABLE_START)
    end = lines.index(_PROPAGATION_TABLE_END, start)
    for index in range(start + 1, end):
        for target, replacement in replacements.items():
            if lines[index].startswith(f"| `{target}` |"):
                lines[index] = replacement
                break
    return "\n".join(lines).rstrip() + "\n"


def record_propagation_outcomes(
    body: str, outcomes: Iterable[PropagationOutcome]
) -> str:
    replacements = {}
    for outcome in outcomes:
        if isinstance(outcome, PropagationResult):
            replacement = _table_row(
                outcome.request.target,
                "pr",
                "Recorded",
                f"[{outcome.pr}]({outcome.url})",
            )
        else:
            reason = outcome.reason.replace("|", "\\|")
            replacement = _table_row(
                outcome.request.target,
                "pr",
                f"Skipped — {reason}",
            )
        replacements[outcome.request.target] = replacement
    return _replace_propagation_rows(body, replacements)


def build_tracking_issue(
    change: CanonicalChange,
    labels: LabelResolution,
    manifest: Manifest,
    decisions: Sequence[Decision] = (),
    queued_targets: Iterable[str] = (),
) -> str:
    categories: dict[FileCategory, list[PullRequestFile]] = {
        category: [] for category in FileCategory
    }
    for file in change.files:
        categories[classify_file(file)].append(file)

    lines = [
        change.marker,
        f"# Impact plan: {change.source_id} — {change.title}",
        "",
        f"Canonical change: [{change.repo}#{change.number}]({change.url}) at `{change.sha}`.",
        "",
        "> Agricola creates draft downstream PRs only for targets authorized by merge-time labels or maintainer commands.",
        "",
        "## Change classification",
        "",
    ]
    lines.extend(
        _section(
            "Normative specification requirements — must propagate",
            categories[FileCategory.NORMATIVE],
            "No specification or conformance files detected.",
        )
    )
    lines.extend(
        _section(
            "Canonical behavior worth matching — should propagate",
            categories[FileCategory.CANONICAL],
            "No canonical implementation files detected.",
        )
    )
    lines.extend(
        _section(
            "Incidental TypeScript or repository details — should not propagate",
            categories[FileCategory.INCIDENTAL],
            "No incidental files detected.",
        )
    )
    lines.extend(["## Label decision", ""])
    lines.append(
        f"- Authorized merge-time labels: {', '.join(f'`{label}`' for label in labels.labels) or 'none'}"
    )
    selected = ", ".join(f"`{target}`" for target in labels.targets)
    if not selected:
        selected = (
            "none (propagation disabled)"
            if labels.disabled
            else "none (awaiting command)"
        )
    lines.append(f"- Selected targets: {selected}")
    for error in labels.errors:
        lines.append(f"- Error: **{error}**")
    for note in labels.notes:
        lines.append(f"- Conflict resolution: {note}")
    pr_targets = ", ".join(f"`{target}`" for target in manifest.pr_targets())
    notify_targets = ", ".join(
        f"`{name}`"
        for name, sdk in manifest.sdks.items()
        if sdk.automation is Automation.NOTIFY
    )
    lines.extend(
        [
            "",
            "## Target inventory",
            "",
            f"- Draft PR automation: {pr_targets or 'none'}.",
            f"- Notification only: {notify_targets or 'none'}.",
        ]
    )
    lines.extend(["", "## Downstream propagation", ""])
    lines.extend(
        _propagation_table(
            manifest,
            (*labels.targets, *queued_targets),
            decisions,
        )
    )
    lines.extend(
        [
            "",
            "## Commands",
            "",
            *quick_fix_lines(),
            "",
            "Post one of these as an issue comment. `/ag` must be the first token on the line.",
            "",
            "| Command | What it does | Example |",
            "| --- | --- | --- |",
            "| `plan` | Rebuilds this impact plan from the immutable merge-time snapshot. | `/ag plan` |",
            '| `fix [sdk...] ["instruction"]` | Queues named PR-enabled SDKs, or every PR-enabled SDK when omitted. For a recorded PR, a quoted instruction revises it using review feedback and failed CI. | `/ag fix python "address the review comments"` |',
            "| `status` | Reports the current state of recorded downstream PRs. | `/ag status` |",
            '| `skip <sdk> reason="..."` | Records why one SDK should not receive this change. | `/ag skip go reason="Not applicable to this transport"` |',
            "",
            "Generated changes remain draft until a maintainer reviews and merges them.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def tracking_issue_title(change: CanonicalChange) -> str:
    return f"[Agricola] {change.source_id}: {change.title}"
