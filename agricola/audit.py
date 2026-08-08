from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .ledger import AuditStore
from .models import (
    AuditCheckStatus,
    AuditFinding,
    AuditObservation,
    AuditReport,
    AuditSnapshot,
    Manifest,
    PendingAuditReport,
)

AUDIT_MARKER = "<!-- agricola:audit -->"
AUDIT_TITLE = "[Agricola] SDK drift audit"


class AuditError(ValueError):
    pass


class GitHub(Protocol):
    def find_tracking_issue(self, marker: str) -> dict[str, object] | None: ...
    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> dict[str, object]: ...
    def update_issue(
        self, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class FindingDraft:
    fingerprint: str
    summary: str
    reference: str | None
    affected: tuple[str, ...]
    clean: tuple[str, ...]
    likely_origin: str


@dataclass
class _FindingGroup:
    summary: str
    reference: str | None
    affected: set[str]
    clean: set[str]


def audit_matrix(
    manifest: Manifest, adapters_root: str | Path
) -> dict[str, list[dict[str, str]]]:
    root = Path(adapters_root)
    include = []
    missing = []
    for target, sdk in manifest.sdks.items():
        if not (root / target / "adapter.json").is_file():
            missing.append(target)
            continue
        include.append(
            {
                "target": target,
                "repo": sdk.repo,
                "automation": sdk.automation.value,
            }
        )
    if missing:
        raise AuditError(
            "manifest targets have no conformance adapter: " + ", ".join(missing)
        )
    return {"include": include}


def snapshot_from_conformance(
    *,
    target: str,
    repo: str,
    sha: str,
    adapter_manifest: Mapping[str, object],
    results: Mapping[str, object],
) -> AuditSnapshot:
    capabilities = adapter_manifest.get("capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) and item for item in capabilities
    ):
        raise AuditError("adapter manifest capabilities must be non-empty strings")
    raw_checks = results.get("checks")
    if not isinstance(raw_checks, list):
        raise AuditError("conformance results checks must be an array")

    observations: list[AuditObservation] = []
    errors: list[str] = []
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise AuditError("conformance result check must be an object")
        details = raw.get("details")
        if not isinstance(details, dict):
            raise AuditError("conformance result check details must be an object")
        vector = str(details.get("vector") or "")
        scenario = str(details.get("scenario") or "")
        test_type = str(details.get("testType") or "")
        if not vector or not scenario or not test_type:
            raise AuditError("conformance check is missing vector identity")
        status = raw.get("status")
        try:
            check_status = AuditCheckStatus(str(status))
        except ValueError as exc:
            raise AuditError(f"invalid conformance check status: {status!r}") from exc
        summary = str(raw.get("description") or raw.get("name") or scenario)
        if vector in {"adapter", "runner"}:
            if check_status is AuditCheckStatus.FAILURE:
                errors.append(f"{target}: {summary}")
            continue
        references = raw.get("specReferences")
        reference = None
        if isinstance(references, list) and references:
            first = references[0]
            if isinstance(first, dict) and first.get("id"):
                reference = str(first["id"])
        observations.append(
            AuditObservation(
                fingerprint=(
                    f"vector:{_component(vector)}/{_component(scenario)}/"
                    f"{_component(test_type)}"
                ),
                status=check_status,
                summary=summary,
                reference=reference,
            )
        )
    if not observations:
        errors.append(f"{target}: no protocol conformance checks completed")
    return AuditSnapshot(
        target=target,
        repo=repo,
        sha=sha,
        capabilities=frozenset(capabilities),
        observations=tuple(observations),
        errors=tuple(errors),
    )


def analyze_deltas(
    canonical: AuditSnapshot,
    targets: Sequence[AuditSnapshot],
) -> tuple[tuple[FindingDraft, ...], tuple[str, ...]]:
    errors = list(canonical.errors)
    errors.extend(error for target in targets for error in target.errors)
    canonical_checks = {
        observation.fingerprint: observation for observation in canonical.observations
    }
    for observation in canonical.observations:
        if observation.status is AuditCheckStatus.FAILURE:
            errors.append(
                f"canonical reference failed {observation.fingerprint}: "
                f"{observation.summary}"
            )

    valid_targets = tuple(target for target in targets if not target.errors)
    groups: dict[str, _FindingGroup] = {}

    for capability in sorted(canonical.capabilities):
        affected = {
            target.target
            for target in valid_targets
            if capability not in target.capabilities
        }
        if affected:
            fingerprint = f"capability:{capability}"
            groups[fingerprint] = _FindingGroup(
                summary=f"Canonical operation `{capability}` is not declared",
                reference=capability,
                affected=affected,
                clean={
                    target.target
                    for target in valid_targets
                    if capability in target.capabilities
                },
            )

    for target in valid_targets:
        for observation in target.observations:
            canonical_observation = canonical_checks.get(observation.fingerprint)
            if (
                observation.status is not AuditCheckStatus.FAILURE
                or canonical_observation is None
                or canonical_observation.status is not AuditCheckStatus.SUCCESS
            ):
                continue
            group = groups.setdefault(
                observation.fingerprint,
                _FindingGroup(
                    summary=canonical_observation.summary,
                    reference=canonical_observation.reference,
                    affected=set(),
                    clean=set(),
                ),
            )
            group.affected.add(target.target)

    for group_fingerprint, group in groups.items():
        if group_fingerprint.startswith("capability:"):
            continue
        group.clean.update(
            target.target
            for target in valid_targets
            if any(
                observation.fingerprint == group_fingerprint
                and observation.status is AuditCheckStatus.SUCCESS
                for observation in target.observations
            )
        )

    findings = []
    for fingerprint, group in sorted(groups.items()):
        affected = tuple(sorted(group.affected))
        clean = tuple(sorted(group.clean))
        findings.append(
            FindingDraft(
                fingerprint=fingerprint,
                summary=group.summary,
                reference=group.reference,
                affected=affected,
                clean=clean,
                likely_origin=_likely_origin(len(affected), len(valid_targets)),
            )
        )
    return tuple(findings), tuple(dict.fromkeys(errors))


def build_audit_report(
    manifest: Manifest,
    snapshots: Sequence[AuditSnapshot],
    store: AuditStore,
    *,
    at: datetime | None = None,
) -> AuditReport:
    generated_at = (at or datetime.now(UTC)).astimezone(UTC)
    by_target = {snapshot.target: snapshot for snapshot in snapshots}
    canonical = by_target.get("typescript")
    if canonical is None:
        raise AuditError("canonical typescript snapshot is missing")
    missing = [target for target in manifest.sdks if target not in by_target]
    targets = tuple(
        by_target[target] for target in manifest.sdks if target in by_target
    )
    drafts, analysis_errors = analyze_deltas(canonical, targets)
    errors = (
        *(f"{target}: audit snapshot is missing" for target in missing),
        *analysis_errors,
    )
    ids = store.assign((draft.fingerprint for draft in drafts), generated_at)
    findings = tuple(
        AuditFinding(
            id=ids[draft.fingerprint],
            fingerprint=draft.fingerprint,
            summary=draft.summary,
            reference=draft.reference,
            affected=draft.affected,
            clean=draft.clean,
            likely_origin=draft.likely_origin,
        )
        for draft in drafts
    )
    return AuditReport(
        generated_at=generated_at,
        canonical=canonical,
        targets=targets,
        findings=findings,
        errors=tuple(errors),
    )


def render_audit_report(report: AuditReport) -> PendingAuditReport:
    timestamp = report.generated_at.isoformat().replace("+00:00", "Z")
    lines = [
        AUDIT_MARKER,
        "# SDK drift audit",
        "",
        f"Head-to-head audit generated at `{timestamp}`.",
        "",
        "> Findings are advisory and never create downstream changes automatically.",
        "",
        "## Audited heads",
        "",
        "| Target | Repository | Commit | Health |",
        "| --- | --- | --- | --- |",
        _snapshot_row(report.canonical),
        *(_snapshot_row(target) for target in report.targets),
        "",
        "## Findings",
        "",
    ]
    if report.findings:
        lines.extend(
            [
                "| Finding | Fingerprint | Affected | Clean | Likely origin |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for finding in report.findings:
            lines.append(
                "| {id} | `{fingerprint}` | {affected} | {clean} | {origin} |".format(
                    id=finding.id,
                    fingerprint=_escape(finding.fingerprint),
                    affected=", ".join(f"`{item}`" for item in finding.affected),
                    clean=", ".join(f"`{item}`" for item in finding.clean) or "—",
                    origin=_escape(finding.likely_origin),
                )
            )
        for finding in report.findings:
            lines.extend(
                [
                    "",
                    f"### {finding.id} — `{finding.fingerprint}`",
                    "",
                    finding.summary,
                    "",
                    f"- Affected: {', '.join(f'`{item}`' for item in finding.affected)}",
                    f"- Clean: {', '.join(f'`{item}`' for item in finding.clean) or 'none'}",
                    f"- Canonical reference: `{finding.reference or 'conformance result'}`",
                    f"- Likely origin: {finding.likely_origin}",
                ]
            )
    else:
        lines.append("No deterministic capability or conformance deltas detected.")

    lines.extend(["", "## Audit health", ""])
    if report.errors:
        lines.extend(f"- {error}" for error in report.errors)
    else:
        lines.append("All configured SDK snapshots completed successfully.")
    lines.extend(
        [
            "",
            "This roll-up is updated in place. Individual SDK issues and propagation "
            "remain explicit maintainer actions.",
        ]
    )
    return PendingAuditReport(
        title=AUDIT_TITLE,
        body="\n".join(lines).rstrip() + "\n",
        healthy=not report.errors,
    )


def deliver_audit_report(
    client: GitHub, pending: PendingAuditReport
) -> dict[str, object]:
    existing = client.find_tracking_issue(AUDIT_MARKER)
    if existing is None:
        return client.create_issue(pending.title, pending.body)
    return client.update_issue(
        int(str(existing["number"])),
        title=pending.title,
        body=pending.body,
    )


def load_snapshots(directory: str | Path) -> tuple[AuditSnapshot, ...]:
    snapshots = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            snapshots.append(AuditSnapshot.model_validate_json(path.read_text()))
        except (OSError, ValidationError) as exc:
            raise AuditError(f"cannot read audit snapshot {path}: {exc}") from exc
    targets = [snapshot.target for snapshot in snapshots]
    if len(targets) != len(set(targets)):
        raise AuditError("audit snapshots contain duplicate targets")
    return tuple(snapshots)


def read_json_object(path: str | Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"{path} must contain a JSON object")
    return value


def _component(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-") or "unknown"


def _likely_origin(affected: int, total: int) -> str:
    if total > 1 and affected == total - 1:
        return "likely canonical change that did not fan out"
    if affected == 1:
        return "likely SDK-local divergence"
    return "shared downstream divergence"


def _escape(value: str) -> str:
    return value.replace("|", "\\|")


def _snapshot_row(snapshot: AuditSnapshot) -> str:
    link = f"https://github.com/{snapshot.repo}/commit/{snapshot.sha}"
    health = "Incomplete" if snapshot.errors else "Complete"
    return (
        f"| `{snapshot.target}` | `{snapshot.repo}` | "
        f"[`{snapshot.sha[:12]}`]({link}) | {health} |"
    )
