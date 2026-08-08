from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agricola.audit import (
    AUDIT_MARKER,
    analyze_deltas,
    audit_finding_context_from_body,
    audit_matrix,
    build_audit_report,
    deliver_audit_report,
    ensure_audit_remediation,
    render_audit_report,
    snapshot_from_conformance,
)
from agricola.ledger import AuditStore
from agricola.models import (
    AuditCheckStatus,
    AuditCodeEvidence,
    AuditConfidence,
    AuditObservation,
    AuditSemanticFinding,
    AuditSemanticResult,
    AuditSeverity,
    AuditSnapshot,
)
from agricola.tests.helpers import manifest


class AuditGitHub:
    def __init__(self, issues: tuple[dict[str, object], ...] = ()) -> None:
        self.issues = [dict(issue) for issue in issues]
        self.updated: list[tuple[int, str | None, str | None, str | None]] = []

    def find_tracking_issues(self, marker: str) -> tuple[dict[str, object], ...]:
        return tuple(
            issue
            for issue in self.issues
            if marker in str(issue.get("body") or "")
        )

    def create_issue(self, title: str, body: str, labels=()):
        number = max(
            (int(str(issue["number"])) for issue in self.issues), default=0
        ) + 1
        issue: dict[str, object] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "html_url": f"https://github.com/tempoxyz/mpp-tools/issues/{number}",
        }
        self.issues.append(issue)
        return issue

    def update_issue(self, number, *, title=None, body=None, state=None):
        issue = next(item for item in self.issues if item["number"] == number)
        if title is not None:
            issue["title"] = title
        if body is not None:
            issue["body"] = body
        if state is not None:
            issue["state"] = state
        self.updated.append((number, title, body, state))
        return issue


def observation(
    fingerprint: str,
    status: AuditCheckStatus = AuditCheckStatus.SUCCESS,
) -> AuditObservation:
    return AuditObservation(
        fingerprint=fingerprint,
        status=status,
        summary=f"Check {fingerprint}",
        reference="MPP-1",
    )


def snapshot(
    target: str,
    *,
    capabilities: tuple[str, ...] = ("challenge.parse", "receipt.parse"),
    observations: tuple[AuditObservation, ...] = (),
    semantic_findings: tuple[AuditSemanticFinding, ...] = (),
    semantic_reviewed: bool | None = None,
    errors: tuple[str, ...] = (),
) -> AuditSnapshot:
    repos = {
        "typescript": "wevm/mppx",
        "go": "tempoxyz/mpp-go",
        "rust": "tempoxyz/mpp-rs",
        "ruby": "stripe/mpp-rb",
    }
    reviewed = (
        target != "typescript" if semantic_reviewed is None else semantic_reviewed
    )
    return AuditSnapshot(
        target=target,
        repo=repos[target],
        sha=f"{target}1234567",
        capabilities=frozenset(capabilities),
        observations=observations,
        semantic_reviewed=reviewed,
        semantic_summary="Repository comparison completed" if reviewed else None,
        semantic_findings=semantic_findings,
        errors=errors,
    )


def semantic_finding(
    fingerprint: str = "semantic:receipt/verification-order",
    *,
    severity: AuditSeverity = AuditSeverity.MEDIUM,
    confidence: AuditConfidence = AuditConfidence.HIGH,
) -> AuditSemanticFinding:
    return AuditSemanticFinding(
        fingerprint=fingerprint,
        title="Receipt verification order differs",
        description="The SDK validates the signature after accepting the receipt.",
        severity=severity,
        confidence=confidence,
        canonical=AuditCodeEvidence(
            path="src/receipt.ts",
            line=42,
            symbol="verifyReceipt",
            behavior="Verifies the signature before returning a receipt.",
        ),
        target=AuditCodeEvidence(
            path="src/receipt.rs",
            line=27,
            symbol="verify_receipt",
            behavior="Returns before signature verification.",
        ),
        spec_reference="MPP-4",
        suggested_test="Reject a receipt with an invalid signature.",
    )


class AuditTests(unittest.TestCase):
    def test_semantic_output_schema_is_strict(self) -> None:
        schema = AuditSemanticResult.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "schema_version",
                "target",
                "canonical_sha",
                "target_sha",
                "summary",
                "findings",
            },
        )

    def test_normalizes_conformance_results(self) -> None:
        result = snapshot_from_conformance(
            target="rust",
            repo="tempoxyz/mpp-rs",
            sha="abc1234567",
            adapter_manifest={"capabilities": ["challenge.parse"]},
            results={
                "checks": [
                    {
                        "name": "rust challenge",
                        "description": "Parses challenge",
                        "status": "FAILURE",
                        "specReferences": [{"id": "MPP-1"}],
                        "details": {
                            "vector": "www-authenticate",
                            "scenario": "basic challenge",
                            "testType": "parse",
                        },
                    },
                    {
                        "name": "build",
                        "description": "Adapter builds",
                        "status": "FAILURE",
                        "details": {
                            "vector": "adapter",
                            "scenario": "build",
                            "testType": "build",
                        },
                    },
                ]
            },
        )

        self.assertEqual(result.capabilities, {"challenge.parse"})
        self.assertEqual(
            result.observations[0].fingerprint,
            "vector:www-authenticate/basic-challenge/parse",
        )
        self.assertEqual(result.errors, ("rust: Adapter builds",))

    def test_clusters_capability_and_vector_deltas(self) -> None:
        check = "vector:www-authenticate/basic/parse"
        canonical = snapshot(
            "typescript",
            observations=(observation(check),),
        )
        go = snapshot(
            "go",
            capabilities=("challenge.parse",),
            observations=(observation(check, AuditCheckStatus.FAILURE),),
        )
        rust = snapshot("rust", observations=(observation(check),))

        findings, errors = analyze_deltas(canonical, (go, rust))

        self.assertFalse(errors)
        by_fingerprint = {finding.fingerprint: finding for finding in findings}
        self.assertEqual(
            by_fingerprint["capability:receipt.parse"].affected,
            ("go",),
        )
        self.assertEqual(by_fingerprint[check].affected, ("go",))
        self.assertEqual(by_fingerprint[check].clean, ("rust",))
        self.assertEqual(
            by_fingerprint[check].likely_origin,
            "likely canonical change that did not fan out",
        )

    def test_normalizes_and_clusters_semantic_findings(self) -> None:
        canonical_sha = "canonical123"
        target_sha = "rust1234567"
        raw_finding = semantic_finding().model_dump(mode="json")
        rust = snapshot_from_conformance(
            target="rust",
            repo="tempoxyz/mpp-rs",
            sha=target_sha,
            canonical_sha=canonical_sha,
            adapter_manifest={"capabilities": ["receipt.parse"]},
            results={"checks": [self.vector_check("SUCCESS")]},
            semantic_result={
                "schema_version": 1,
                "target": "rust",
                "canonical_sha": canonical_sha,
                "target_sha": target_sha,
                "summary": "Compared public receipt behavior.",
                "findings": [raw_finding],
            },
        )
        go = snapshot("go", semantic_findings=(semantic_finding(),))
        rust = rust.model_copy(
            update={
                "semantic_findings": (
                    semantic_finding(
                        severity=AuditSeverity.HIGH,
                        confidence=AuditConfidence.LOW,
                    ),
                )
            }
        )
        ruby = snapshot("ruby")

        findings, errors = analyze_deltas(snapshot("typescript"), (go, rust, ruby))

        self.assertFalse(errors)
        semantic = next(
            finding
            for finding in findings
            if finding.fingerprint == "semantic:receipt/verification-order"
        )
        self.assertEqual(semantic.affected, ("go", "rust"))
        self.assertEqual(semantic.clean, ("ruby",))
        self.assertEqual(semantic.severity, AuditSeverity.HIGH)
        self.assertEqual(semantic.confidence, AuditConfidence.LOW)
        self.assertEqual(
            tuple(item.target for item in semantic.semantic_evidence),
            ("go", "rust"),
        )

    def test_invalid_semantic_identity_marks_snapshot_incomplete(self) -> None:
        result = snapshot_from_conformance(
            target="rust",
            repo="tempoxyz/mpp-rs",
            sha="rust1234567",
            canonical_sha="canonical123",
            adapter_manifest={"capabilities": ["receipt.parse"]},
            results={"checks": [self.vector_check("SUCCESS")]},
            semantic_result={
                "schema_version": 1,
                "target": "rust",
                "canonical_sha": "wrong-sha",
                "target_sha": "rust1234567",
                "summary": "Compared implementations.",
                "findings": [],
            },
        )

        self.assertFalse(result.semantic_reviewed)
        self.assertIn("canonical SHA does not match", result.semantic_error or "")

    def test_vector_clean_requires_an_explicit_success(self) -> None:
        check = "vector:www-authenticate/basic/parse"
        canonical = snapshot(
            "typescript",
            observations=(observation(check),),
        )
        go = snapshot(
            "go",
            observations=(observation(check, AuditCheckStatus.FAILURE),),
        )
        rust = snapshot("rust", observations=(observation(check),))
        ruby = snapshot("ruby")

        findings, _ = analyze_deltas(canonical, (go, rust, ruby))

        finding = next(item for item in findings if item.fingerprint == check)
        self.assertEqual(finding.clean, ("rust",))
        self.assertEqual(finding.summary, "Check " + check)

    def test_assigns_stable_ids_and_renders_finding_issues(self) -> None:
        check = "vector:www-authenticate/basic/parse"
        canonical = snapshot("typescript", observations=(observation(check),))
        go = snapshot(
            "go",
            observations=(observation(check, AuditCheckStatus.FAILURE),),
        )
        rust = snapshot("rust", observations=(observation(check),))
        ruby = snapshot("ruby", observations=(observation(check),))
        at = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)

        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(directory)
            first = build_audit_report(
                manifest(), (canonical, go, rust, ruby), store, at=at
            )
            second = build_audit_report(
                manifest(), (canonical, go, rust, ruby), store, at=at
            )

            self.assertEqual(first.findings[0].id, "AGR-2026-001")
            self.assertEqual(second.findings[0].id, "AGR-2026-001")
            pending = render_audit_report(first, manifest())
            self.assertTrue(pending.healthy)
            self.assertIn(AUDIT_MARKER, pending.body)
            self.assertIn("AGR-2026-001", pending.body)
            self.assertIn("`go`", pending.body)
            self.assertEqual(len(pending.finding_issues), 1)
            self.assertIn("## How to action", pending.finding_issues[0].body)
            self.assertIn("## Agricola remediation", pending.finding_issues[0].body)
            self.assertIn("```text\n/agricola fix\n```", pending.finding_issues[0].body)
            self.assertIn("gh workflow run agricola-audit.yml", pending.finding_issues[0].body)
            context = audit_finding_context_from_body(pending.finding_issues[0].body)
            assert context is not None
            self.assertEqual(context.id, "AGR-2026-001")
            self.assertEqual(tuple(context.affected), ("go",))

            legacy = pending.finding_issues[0].body.replace(
                "/agricola fix", "@agricola propagate go"
            )
            updated = ensure_audit_remediation(legacy, context, manifest())
            self.assertIn("```text\n/agricola fix\n```", updated)
            self.assertNotIn("@agricola propagate go", updated)

    def test_rollup_links_semantic_source_evidence(self) -> None:
        canonical = snapshot("typescript")
        rust = snapshot("rust", semantic_findings=(semantic_finding(),))
        go = snapshot("go")
        ruby = snapshot("ruby")

        with tempfile.TemporaryDirectory() as directory:
            report = build_audit_report(
                manifest(), (canonical, go, rust, ruby), AuditStore(directory)
            )

        pending = render_audit_report(report, manifest())
        body = pending.finding_issues[0].body
        self.assertIn("semantic:receipt/verification-order", pending.body)
        self.assertIn("wevm/mppx/blob/typescript1234567/src/receipt.ts#L42", body)
        self.assertIn("tempoxyz/mpp-rs/blob/rust1234567/src/receipt.rs#L27", body)

    def test_missing_snapshot_marks_report_incomplete(self) -> None:
        canonical = snapshot("typescript")
        with tempfile.TemporaryDirectory() as directory:
            report = build_audit_report(manifest(), (canonical,), AuditStore(directory))

        pending = render_audit_report(report, manifest())
        self.assertFalse(pending.healthy)
        self.assertIn("go: audit snapshot is missing", pending.body)

    def test_missing_semantic_review_marks_report_incomplete(self) -> None:
        canonical = snapshot("typescript")
        go = snapshot("go", semantic_reviewed=False)
        rust = snapshot("rust")
        ruby = snapshot("ruby")
        with tempfile.TemporaryDirectory() as directory:
            report = build_audit_report(
                manifest(), (canonical, go, rust, ruby), AuditStore(directory)
            )

        pending = render_audit_report(report, manifest())
        self.assertFalse(pending.healthy)
        self.assertIn("go: semantic review is missing", pending.body)

    def test_matrix_requires_every_manifest_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for target in manifest().sdks:
                path = root / target
                path.mkdir()
                (path / "adapter.json").touch()

            result = audit_matrix(manifest(), root)

        self.assertEqual(
            [item["target"] for item in result["include"]],
            ["go", "rust", "ruby"],
        )

    def test_delivery_updates_existing_rollup(self) -> None:
        client = AuditGitHub(
            (
                {
                    "number": 42,
                    "body": AUDIT_MARKER,
                    "state": "open",
                    "html_url": "https://github.com/tempoxyz/mpp-tools/issues/42",
                },
            )
        )
        pending = render_audit_report(AuditReportFixture.complete(), manifest())

        deliver_audit_report(client, pending)

        self.assertEqual(client.updated[0][0], 42)
        self.assertIn(AUDIT_MARKER, client.updated[0][2] or "")

    def test_delivery_creates_durable_finding_and_links_rollup(self) -> None:
        check = "vector:www-authenticate/basic/parse"
        canonical = snapshot("typescript", observations=(observation(check),))
        go = snapshot(
            "go", observations=(observation(check, AuditCheckStatus.FAILURE),)
        )
        with tempfile.TemporaryDirectory() as directory:
            report = build_audit_report(
                manifest(),
                (canonical, go, snapshot("rust"), snapshot("ruby")),
                AuditStore(directory),
                at=datetime(2026, 8, 8, 18, 0, tzinfo=UTC),
            )
        client = AuditGitHub()

        rollup = deliver_audit_report(client, render_audit_report(report, manifest()))

        finding = next(
            issue for issue in client.issues if "agricola:audit-finding" in str(issue["body"])
        )
        self.assertIn("AGR-2026-001", str(finding["title"]))
        self.assertIn(f"[AGR-2026-001]({finding['html_url']})", str(rollup["body"]))

    def test_healthy_delivery_closes_resolved_findings(self) -> None:
        marker = "<!-- agricola:audit-finding=AGR-2026-001 -->"
        client = AuditGitHub(
            (
                {
                    "number": 7,
                    "body": marker,
                    "state": "open",
                    "html_url": "https://github.com/tempoxyz/mpp-tools/issues/7",
                },
            )
        )
        pending = render_audit_report(AuditReportFixture.complete(), manifest())

        deliver_audit_report(client, pending)

        self.assertEqual(client.issues[0]["state"], "closed")

    def test_delivery_reopens_recurring_finding(self) -> None:
        check = "vector:www-authenticate/basic/parse"
        canonical = snapshot("typescript", observations=(observation(check),))
        go = snapshot(
            "go", observations=(observation(check, AuditCheckStatus.FAILURE),)
        )
        with tempfile.TemporaryDirectory() as directory:
            pending = render_audit_report(
                build_audit_report(
                    manifest(),
                    (canonical, go, snapshot("rust"), snapshot("ruby")),
                    AuditStore(directory),
                    at=datetime(2026, 8, 8, 18, 0, tzinfo=UTC),
                ),
                manifest(),
            )
        finding = pending.finding_issues[0]
        client = AuditGitHub(
            (
                {
                    "number": 7,
                    "body": finding.body,
                    "state": "closed",
                    "html_url": "https://github.com/tempoxyz/mpp-tools/issues/7",
                },
            )
        )

        deliver_audit_report(client, pending)

        self.assertEqual(client.issues[0]["state"], "open")
        self.assertEqual(len(client.issues), 2)

    def test_delivery_preserves_recorded_remediation(self) -> None:
        check = "vector:www-authenticate/basic/parse"
        canonical = snapshot("typescript", observations=(observation(check),))
        go = snapshot(
            "go", observations=(observation(check, AuditCheckStatus.FAILURE),)
        )
        with tempfile.TemporaryDirectory() as directory:
            pending = render_audit_report(
                build_audit_report(
                    manifest(),
                    (canonical, go, snapshot("rust"), snapshot("ruby")),
                    AuditStore(directory),
                ),
                manifest(),
            )
        finding = pending.finding_issues[0]
        recorded = finding.body.replace(
            "| `go` | pr | Awaiting decision | — |",
            "| `go` | pr | Recorded | [tempoxyz/mpp-go#91](https://github.com/tempoxyz/mpp-go/pull/91) |",
        )
        client = AuditGitHub(
            (
                {
                    "number": 7,
                    "body": recorded,
                    "state": "open",
                    "html_url": "https://github.com/tempoxyz/mpp-tools/issues/7",
                },
            )
        )

        deliver_audit_report(client, pending)

        self.assertIn("tempoxyz/mpp-go#91", str(client.issues[0]["body"]))

    def test_incomplete_delivery_preserves_unobserved_findings(self) -> None:
        marker = "<!-- agricola:audit-finding=AGR-2026-001 -->"
        client = AuditGitHub(
            (
                {
                    "number": 7,
                    "body": marker,
                    "state": "open",
                    "html_url": "https://github.com/tempoxyz/mpp-tools/issues/7",
                },
            )
        )
        pending = render_audit_report(
            AuditReportFixture.complete(), manifest()
        ).model_copy(
            update={"healthy": False}
        )

        deliver_audit_report(client, pending)

        self.assertEqual(client.issues[0]["state"], "open")

    @staticmethod
    def vector_check(status: str) -> dict[str, object]:
        return {
            "name": "receipt parse",
            "description": "Parses receipt",
            "status": status,
            "details": {
                "vector": "receipt",
                "scenario": "basic",
                "testType": "parse",
            },
        }


class AuditReportFixture:
    @staticmethod
    def complete():
        with tempfile.TemporaryDirectory() as directory:
            return build_audit_report(
                manifest(),
                (
                    snapshot("typescript"),
                    snapshot("go"),
                    snapshot("rust"),
                    snapshot("ruby"),
                ),
                AuditStore(directory),
                at=datetime(2026, 8, 8, 18, 0, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
