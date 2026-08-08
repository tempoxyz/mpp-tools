from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath

from .models import (
    Automation,
    CanonicalChange,
    LabelResolution,
    Manifest,
    PullRequestFile,
)


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


def build_tracking_issue(
    change: CanonicalChange, labels: LabelResolution, manifest: Manifest
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
    lines.extend(
        [
            "",
            "## Commands",
            "",
            "```text",
            "@agricola plan",
            "@agricola propagate <sdk> [<sdk> ...]",
            "@agricola propagate all",
            "@agricola status",
            '@agricola skip <sdk> reason="..."',
            "```",
            "",
            "Generated changes remain draft until a maintainer reviews and merges them.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def tracking_issue_title(change: CanonicalChange) -> str:
    return f"[Agricola] {change.source_id}: {change.title}"
