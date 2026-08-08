from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath

from .models import (
    SDK,
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
_WORD = re.compile(r"[a-z0-9]+")


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


def detected_capabilities(
    change: CanonicalChange, manifest: Manifest
) -> tuple[str, ...]:
    change_words = _words(
        " ".join((change.title, change.body, *(file.path for file in change.files)))
    )
    capabilities = {
        capability for sdk in manifest.sdks.values() for capability in sdk.capabilities
    }
    return tuple(
        sorted(
            capability
            for capability in capabilities
            if change_words & _words(capability)
        )
    )


def _words(value: str) -> set[str]:
    return {_singular(word) for word in _WORD.findall(value.lower())}


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def _applicability(
    sdk: SDK,
    signals: tuple[str, ...],
    *,
    normative_change: bool,
) -> str:
    if sdk.automation is Automation.NOTIFY:
        return "notify only"
    if normative_change:
        return "applicable: normative or conformance files changed"
    if not signals:
        return "applicability unknown: no declared capability signal detected"
    missing = tuple(signal for signal in signals if signal not in sdk.capabilities)
    if missing:
        return "not applicable: missing declared " + ", ".join(
            f"`{capability}`" for capability in missing
        )
    return "applicable: supports " + ", ".join(
        f"`{capability}`" for capability in signals
    )


def build_tracking_issue(
    change: CanonicalChange, labels: LabelResolution, manifest: Manifest
) -> str:
    categories: dict[FileCategory, list[PullRequestFile]] = {
        category: [] for category in FileCategory
    }
    for file in change.files:
        categories[classify_file(file)].append(file)
    capability_signals = detected_capabilities(change, manifest)

    lines = [
        change.marker,
        f"# Impact plan: {change.source_id} — {change.title}",
        "",
        f"Canonical change: [{change.repo}#{change.number}]({change.url}) at `{change.sha}`.",
        "",
        "> This is a dry-run plan. Agricola never writes to downstream SDK repositories.",
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
    lines.extend(["", "## SDK applicability", ""])
    lines.append(
        "- Detected capability signals: "
        + (", ".join(f"`{capability}`" for capability in capability_signals) or "none")
    )
    lines.append("")
    for name, sdk in manifest.sdks.items():
        disposition = _applicability(
            sdk,
            capability_signals,
            normative_change=bool(categories[FileCategory.NORMATIVE]),
        )
        authorization = (
            "selected by authorized label" if name in labels.targets else "not selected"
        )
        capabilities = (
            ", ".join(f"`{capability}`" for capability in sdk.capabilities)
            or "not declared"
        )
        lines.append(
            f"- **{name}** (`{sdk.repo}`): {disposition}; {authorization}. "
            f"Capabilities: {capabilities}."
        )
    lines.extend(
        [
            "",
            "## Commands",
            "",
            "```text",
            "@agricola plan",
            "@agricola status",
            '@agricola skip <sdk> reason="..."',
            "```",
            "",
            "Propagation and revision commands are intentionally unavailable.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def tracking_issue_title(change: CanonicalChange) -> str:
    return f"[Agricola] {change.source_id}: {change.title}"
