from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .github import agricola_issue_labels
from .ledger import AuditStore
from .models import (
    AuditFindingContext,
    AuditCheckStatus,
    AuditCodeEvidence,
    AuditConfidence,
    AuditFinding,
    AuditObservation,
    AuditReport,
    AuditSemanticEvidence,
    AuditSemanticFinding,
    AuditSemanticResult,
    AuditSeverity,
    AuditSnapshot,
    AuditTarget,
    Automation,
    Manifest,
    PendingAuditFindingIssue,
    PendingAuditReport,
)
from .planner import preserve_propagation_state, propagation_table, quick_fix_lines

AUDIT_MARKER = "<!-- agricola:audit -->"
AUDIT_TITLE = "[Agricola] SDK drift audit"
AUDIT_FINDING_MARKER_PREFIX = "<!-- agricola:audit-finding="
AUDIT_FINDING_CONTEXT_PREFIX = "<!-- agricola:audit-finding-context="


class AuditError(ValueError):
    pass


class GitHub(Protocol):
    def find_tracking_issues(self, marker: str) -> tuple[dict[str, object], ...]: ...
    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> dict[str, object]: ...
    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class FindingDraft:
    fingerprint: str
    summary: str
    reference: str | None
    affected: tuple[str, ...]
    clean: tuple[str, ...]
    likely_origin: str
    severity: AuditSeverity | None = None
    confidence: AuditConfidence | None = None
    semantic_evidence: tuple[AuditSemanticEvidence, ...] = ()


@dataclass
class _FindingGroup:
    summary: str
    reference: str | None
    affected: set[str]
    clean: set[str]
    semantic_evidence: dict[str, AuditSemanticFinding]


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
    canonical_sha: str | None = None,
    semantic_result: Mapping[str, object] | None = None,
    semantic_error: str | None = None,
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
    semantic_reviewed = False
    semantic_summary = None
    semantic_findings: tuple[AuditSemanticFinding, ...] = ()
    normalized_semantic_error = semantic_error
    if semantic_result is not None:
        try:
            semantic = AuditSemanticResult.model_validate(semantic_result)
            if semantic.target != target:
                raise AuditError(
                    f"semantic review target is {semantic.target}, expected {target}"
                )
            if semantic.target_sha != sha:
                raise AuditError("semantic review target SHA does not match checkout")
            if canonical_sha is None or semantic.canonical_sha != canonical_sha:
                raise AuditError(
                    "semantic review canonical SHA does not match checkout"
                )
            semantic_reviewed = True
            semantic_summary = semantic.summary
            semantic_findings = semantic.findings
            normalized_semantic_error = None
        except (AuditError, ValidationError) as exc:
            normalized_semantic_error = f"{target}: invalid semantic review: {exc}"
    return AuditSnapshot(
        target=target,
        repo=repo,
        sha=sha,
        capabilities=frozenset(capabilities),
        observations=tuple(observations),
        semantic_reviewed=semantic_reviewed,
        semantic_summary=semantic_summary,
        semantic_findings=semantic_findings,
        semantic_error=normalized_semantic_error,
        errors=tuple(errors),
    )


def analyze_deltas(
    canonical: AuditSnapshot,
    targets: Sequence[AuditSnapshot],
) -> tuple[tuple[FindingDraft, ...], tuple[str, ...]]:
    errors = list(canonical.errors)
    errors.extend(error for target in targets for error in target.errors)
    errors.extend(
        target.semantic_error for target in targets if target.semantic_error is not None
    )
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
                semantic_evidence={},
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
                    semantic_evidence={},
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

    semantic_targets = tuple(target for target in targets if target.semantic_reviewed)
    semantic_names = {target.target for target in semantic_targets}
    for target in semantic_targets:
        for finding in target.semantic_findings:
            group = groups.setdefault(
                finding.fingerprint,
                _FindingGroup(
                    summary=finding.title,
                    reference=(
                        finding.spec_reference
                        or f"{finding.canonical.path}:{finding.canonical.line or 1}"
                    ),
                    affected=set(),
                    clean=set(),
                    semantic_evidence={},
                ),
            )
            group.affected.add(target.target)
            group.semantic_evidence[target.target] = finding

    for fingerprint, group in groups.items():
        if fingerprint.startswith("semantic:"):
            group.clean.update(semantic_names - group.affected)

    findings = []
    for fingerprint, group in sorted(groups.items()):
        affected = tuple(sorted(group.affected))
        clean = tuple(sorted(group.clean))
        compared_targets = (
            len(semantic_targets)
            if fingerprint.startswith("semantic:")
            else len(valid_targets)
        )
        findings.append(
            FindingDraft(
                fingerprint=fingerprint,
                summary=group.summary,
                reference=group.reference,
                affected=affected,
                clean=clean,
                likely_origin=_likely_origin(len(affected), compared_targets),
                severity=_highest_severity(group.semantic_evidence.values()),
                confidence=_lowest_confidence(group.semantic_evidence.values()),
                semantic_evidence=tuple(
                    AuditSemanticEvidence(target=target, finding=finding)
                    for target, finding in sorted(group.semantic_evidence.items())
                ),
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
    missing_semantic = [
        target.target
        for target in targets
        if not target.semantic_reviewed and target.semantic_error is None
    ]
    drafts, analysis_errors = analyze_deltas(canonical, targets)
    errors = (
        *(f"{target}: audit snapshot is missing" for target in missing),
        *(f"{target}: semantic review is missing" for target in missing_semantic),
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
            severity=draft.severity,
            confidence=draft.confidence,
            semantic_evidence=draft.semantic_evidence,
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


def render_audit_report(report: AuditReport, manifest: Manifest) -> PendingAuditReport:
    timestamp = report.generated_at.isoformat().replace("+00:00", "Z")
    snapshots = {
        snapshot.target: snapshot for snapshot in (report.canonical, *report.targets)
    }
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
        "| Target | Repository | Commit | Conformance | Semantic review |",
        "| --- | --- | --- | --- | --- |",
        _snapshot_row(report.canonical),
        *(_snapshot_row(target) for target in report.targets),
        "",
        "## Findings",
        "",
    ]
    if report.findings:
        lines.extend(
            [
                "| Finding | Source | Fingerprint | Severity | Affected | Clean | Likely origin |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for finding in report.findings:
            lines.append(
                "| {id} | {source} | `{fingerprint}` | {severity} | {affected} | {clean} | {origin} |".format(
                    id=finding.id,
                    source=_finding_source(finding.fingerprint),
                    fingerprint=_escape(finding.fingerprint),
                    severity=finding.severity or "—",
                    affected=", ".join(f"`{item}`" for item in finding.affected),
                    clean=", ".join(f"`{item}`" for item in finding.clean) or "—",
                    origin=_escape(finding.likely_origin),
                )
            )
        lines.extend(
            [
                "",
                "Each finding links to a durable issue. Assign it, link remediation "
                "pull requests, and use the next healthy audit to verify the fix.",
            ]
        )
    else:
        lines.append(
            "No capability, conformance, or semantic implementation deltas detected."
        )

    lines.extend(["", "## Audit health", ""])
    if report.errors:
        lines.extend(f"- {error}" for error in report.errors)
    else:
        lines.append("All configured SDK snapshots completed successfully.")
    lines.extend(
        [
            "",
            "This index and its finding issues are updated in place. Findings are "
            "closed only after a healthy audit no longer detects them.",
        ]
    )
    finding_issues = tuple(
        _render_finding_issue(report, finding, snapshots, timestamp, manifest)
        for finding in report.findings
    )
    return PendingAuditReport(
        title=AUDIT_TITLE,
        body="\n".join(lines).rstrip() + "\n",
        healthy=not report.errors,
        finding_issues=finding_issues,
    )


def deliver_audit_report(
    client: GitHub, pending: PendingAuditReport
) -> dict[str, object]:
    existing = client.find_tracking_issues("<!-- agricola:audit")
    rollup = next(
        (issue for issue in existing if AUDIT_MARKER in str(issue.get("body") or "")),
        None,
    )
    findings_by_marker = {
        marker: issue
        for issue in existing
        if (marker := _finding_marker_from_body(str(issue.get("body") or "")))
    }
    active_markers = {finding.marker for finding in pending.finding_issues}
    urls: dict[str, str] = {}
    for finding in pending.finding_issues:
        issue = findings_by_marker.get(finding.marker)
        if issue is None:
            issue = client.create_issue(finding.title, finding.body, finding.labels)
        else:
            body = preserve_propagation_state(
                finding.body,
                str(issue.get("body") or ""),
            )
            issue = client.update_issue(
                int(str(issue["number"])),
                title=finding.title,
                body=body,
                state="open",
            )
        if url := issue.get("html_url"):
            urls[finding.id] = str(url)

    if pending.healthy:
        for marker, issue in findings_by_marker.items():
            if marker not in active_markers and issue.get("state") != "closed":
                client.update_issue(int(str(issue["number"])), state="closed")

    body = _link_finding_issues(pending.body, urls)
    if rollup is None:
        return client.create_issue(pending.title, body, agricola_issue_labels())
    return client.update_issue(
        int(str(rollup["number"])),
        title=pending.title,
        body=body,
        state="open",
    )


def _render_finding_issue(
    report: AuditReport,
    finding: AuditFinding,
    snapshots: Mapping[str, AuditSnapshot],
    timestamp: str,
    manifest: Manifest,
) -> PendingAuditFindingIssue:
    marker = f"{AUDIT_FINDING_MARKER_PREFIX}{finding.id} -->"
    context = AuditFindingContext(
        id=finding.id,
        fingerprint=finding.fingerprint,
        canonical=AuditTarget(
            repo=report.canonical.repo,
            sha=report.canonical.sha,
        ),
        affected={
            target: AuditTarget(
                repo=snapshots[target].repo,
                sha=snapshots[target].sha,
            )
            for target in finding.affected
        },
    )
    context_marker = f"{AUDIT_FINDING_CONTEXT_PREFIX}{context.model_dump_json()} -->"
    lines = [
        marker,
        context_marker,
        f"# {finding.id} — {finding.summary}",
        "",
        f"Last observed by the head-to-head audit at `{timestamp}`.",
        "",
        "## Audited heads",
        "",
        "| Target | Repository | Commit | Conformance | Semantic review |",
        "| --- | --- | --- | --- | --- |",
        _snapshot_row(report.canonical),
        *(
            _snapshot_row(snapshots[target])
            for target in (*finding.affected, *finding.clean)
        ),
        "",
        "## Finding",
        "",
        f"- Fingerprint: `{finding.fingerprint}`",
        f"- Source: {_finding_source(finding.fingerprint)}",
        f"- Affected SDKs: {', '.join(f'`{item}`' for item in finding.affected)}",
        f"- Clean SDKs: {', '.join(f'`{item}`' for item in finding.clean) or 'none'}",
        f"- Canonical reference: `{finding.reference or 'conformance result'}`",
        f"- Likely origin: {finding.likely_origin}",
    ]
    if finding.severity is not None:
        lines.append(f"- Severity: {finding.severity}")
    if finding.confidence is not None:
        lines.append(f"- Confidence: {finding.confidence}")

    if finding.semantic_evidence:
        lines.extend(
            [
                "",
                "## Evidence",
                "",
                "| SDK | Canonical evidence | SDK evidence | Suggested test |",
                "| --- | --- | --- | --- |",
            ]
        )
        for evidence in finding.semantic_evidence:
            target = snapshots[evidence.target]
            semantic = evidence.finding
            lines.append(
                "| `{target}` | {canonical} | {downstream} | {test} |".format(
                    target=evidence.target,
                    canonical=_evidence_link(
                        report.canonical.repo,
                        report.canonical.sha,
                        semantic.canonical,
                    ),
                    downstream=_evidence_link(
                        target.repo,
                        target.sha,
                        semantic.target,
                    ),
                    test=_escape(semantic.suggested_test),
                )
            )
        for evidence in finding.semantic_evidence:
            lines.extend(
                [
                    "",
                    f"**{evidence.target}:** {evidence.finding.description}",
                ]
            )

    lines.extend(
        [
            "",
            *_finding_command_section(finding.affected, manifest),
        ]
    )
    title = f"[Agricola] {finding.id}: {finding.summary}"[:256]
    return PendingAuditFindingIssue(
        id=finding.id,
        marker=marker,
        title=title,
        body="\n".join(lines).rstrip() + "\n",
        labels=agricola_issue_labels(finding.affected),
    )


def _finding_marker_from_body(body: str) -> str | None:
    match = re.search(
        r"<!-- agricola:audit-finding=AGR-[0-9]{4}-[0-9]{3,} -->",
        body,
    )
    return match.group(0) if match else None


def audit_finding_context_from_body(body: str) -> AuditFindingContext | None:
    marker = _finding_marker_from_body(body)
    if marker is None:
        return None
    encoded = re.search(
        rf"^{re.escape(AUDIT_FINDING_CONTEXT_PREFIX)}(?P<context>.+) -->$",
        body,
        re.MULTILINE,
    )
    if encoded is not None:
        try:
            return AuditFindingContext.model_validate_json(encoded.group("context"))
        except ValidationError as exc:
            raise AuditError(f"invalid audit finding context: {exc}") from exc

    finding_id = re.search(r"agricola:audit-finding=(AGR-[0-9]{4}-[0-9]{3,})", marker)
    fingerprint = re.search(r"^- Fingerprint: `([^`]+)`$", body, re.MULTILINE)
    affected = re.search(r"^- Affected SDKs: (.+)$", body, re.MULTILINE)
    snapshots = {
        match.group("target"): AuditTarget(
            repo=match.group("repo"),
            sha=match.group("sha"),
        )
        for match in re.finditer(
            r"^\| `(?P<target>[a-z][a-z0-9-]*)` \| "
            r"`(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)` \| "
            r"\[`[^`]+`\]\(https://github\.com/[^/]+/[^/]+/commit/"
            r"(?P<sha>[0-9a-fA-F]{7,40})\)",
            body,
            re.MULTILINE,
        )
    }
    affected_targets = (
        ()
        if affected is None
        else tuple(re.findall(r"`([a-z][a-z0-9-]*)`", affected.group(1)))
    )
    if (
        finding_id is None
        or fingerprint is None
        or "typescript" not in snapshots
        or not affected_targets
        or any(target not in snapshots for target in affected_targets)
    ):
        raise AuditError("audit finding issue is missing its pinned audit context")
    return AuditFindingContext(
        id=finding_id.group(1),
        fingerprint=fingerprint.group(1),
        canonical=snapshots["typescript"],
        affected={target: snapshots[target] for target in affected_targets},
    )


def _finding_command_lines(affected: Iterable[str], manifest: Manifest) -> list[str]:
    affected_targets = tuple(affected)
    enabled = tuple(
        target
        for target in affected_targets
        if manifest.target(target).automation is Automation.PR
    )
    rows = ["| Command | What it does |", "| --- | --- |"]
    if enabled:
        rows.append(
            "| `/ag fix` | Opens or retries draft fixes for every affected "
            "PR-enabled SDK. |"
        )
        rows.extend(
            f"| `/ag fix {target}` | Opens or retries the draft fix for `{target}` only. |"
            for target in enabled
        )
        rows.append(
            '| `/ag fix "instruction"` | Applies the instruction to affected fixes; '
            "recorded PRs also incorporate unresolved review feedback and failed CI. |"
        )
    rows.append(
        "| `/ag status` | Reports the current state of linked remediation pull requests. |"
    )
    notify_only = tuple(target for target in affected_targets if target not in enabled)
    if notify_only:
        names = ", ".join(f"`{target}`" for target in notify_only)
        noun = "SDK is" if len(notify_only) == 1 else "SDKs are"
        rows.extend(
            [
                "",
                f"> `/ag fix` is unavailable for {names} because the affected {noun} "
                "configured for notification-only automation.",
            ]
        )
    return rows


def _finding_command_section(affected: Iterable[str], manifest: Manifest) -> list[str]:
    affected_targets = tuple(affected)
    enabled = tuple(
        target
        for target in affected_targets
        if manifest.target(target).automation is Automation.PR
    )
    lines = [
        "## Available `/ag` commands",
        "",
        "Post a command as a new comment. Only configured maintainers can run "
        "these commands.",
    ]
    if enabled:
        lines.extend(
            [
                "",
                *propagation_table(manifest, affected_targets),
                "",
                *quick_fix_lines(),
            ]
        )
    lines.extend(["", *_finding_command_lines(affected_targets, manifest)])
    return lines


def ensure_audit_remediation(
    body: str,
    context: AuditFindingContext,
    manifest: Manifest,
) -> str:
    section = "\n".join(_finding_command_section(context.affected, manifest))
    headings = (
        "\n## Agricola remediation\n",
        "\n## Available `/ag` commands\n",
        "\n## How to action\n",
    )
    indexes = tuple(index for heading in headings if (index := body.find(heading)) >= 0)
    prefix = body[: min(indexes)].rstrip() if indexes else body.rstrip()
    updated = prefix + "\n\n" + section + "\n"
    return preserve_propagation_state(updated, body)


def _link_finding_issues(body: str, urls: Mapping[str, str]) -> str:
    for finding_id, url in urls.items():
        body = body.replace(
            f"| {finding_id} |",
            f"| [{finding_id}]({url}) |",
            1,
        )
    return body


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


def _highest_severity(
    findings: Iterable[AuditSemanticFinding],
) -> AuditSeverity | None:
    rank = {
        AuditSeverity.LOW: 1,
        AuditSeverity.MEDIUM: 2,
        AuditSeverity.HIGH: 3,
    }
    return max(
        (finding.severity for finding in findings),
        key=rank.__getitem__,
        default=None,
    )


def _lowest_confidence(
    findings: Iterable[AuditSemanticFinding],
) -> AuditConfidence | None:
    rank = {
        AuditConfidence.LOW: 1,
        AuditConfidence.MEDIUM: 2,
        AuditConfidence.HIGH: 3,
    }
    return min(
        (finding.confidence for finding in findings),
        key=rank.__getitem__,
        default=None,
    )


def _escape(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _snapshot_row(snapshot: AuditSnapshot) -> str:
    link = f"https://github.com/{snapshot.repo}/commit/{snapshot.sha}"
    conformance = "Incomplete" if snapshot.errors else "Complete"
    if snapshot.target == "typescript":
        semantic = "Reference"
    else:
        semantic = "Complete" if snapshot.semantic_reviewed else "Incomplete"
    return (
        f"| `{snapshot.target}` | `{snapshot.repo}` | "
        f"[`{snapshot.sha[:12]}`]({link}) | {conformance} | {semantic} |"
    )


def _finding_source(fingerprint: str) -> str:
    return fingerprint.split(":", 1)[0]


def _evidence_link(repo: str, sha: str, evidence: AuditCodeEvidence) -> str:
    line = evidence.line or 1
    url = f"https://github.com/{repo}/blob/{sha}/{evidence.path}#L{line}"
    label = evidence.symbol or f"{evidence.path}:{line}"
    return f"[{_escape(label)}]({url}) — {_escape(evidence.behavior)}"
