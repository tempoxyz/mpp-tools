from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agricola.audit import (
    AUDIT_MARKER,
    analyze_deltas,
    audit_matrix,
    build_audit_report,
    deliver_audit_report,
    render_audit_report,
    snapshot_from_conformance,
)
from agricola.ledger import AuditStore
from agricola.models import (
    AuditCheckStatus,
    AuditObservation,
    AuditSnapshot,
)
from agricola.tests.helpers import manifest
from agricola.tests.test_service import FakeGitHub


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
    errors: tuple[str, ...] = (),
) -> AuditSnapshot:
    repos = {
        "typescript": "wevm/mppx",
        "go": "tempoxyz/mpp-go",
        "rust": "tempoxyz/mpp-rs",
        "ruby": "stripe/mpp-rb",
    }
    return AuditSnapshot(
        target=target,
        repo=repos[target],
        sha=f"{target}1234567",
        capabilities=frozenset(capabilities),
        observations=observations,
        errors=errors,
    )


class AuditTests(unittest.TestCase):
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

    def test_assigns_stable_ids_and_renders_one_rollup(self) -> None:
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
            pending = render_audit_report(first)
            self.assertTrue(pending.healthy)
            self.assertIn(AUDIT_MARKER, pending.body)
            self.assertIn("AGR-2026-001", pending.body)
            self.assertIn("`go`", pending.body)

    def test_missing_snapshot_marks_report_incomplete(self) -> None:
        canonical = snapshot("typescript")
        with tempfile.TemporaryDirectory() as directory:
            report = build_audit_report(manifest(), (canonical,), AuditStore(directory))

        pending = render_audit_report(report)
        self.assertFalse(pending.healthy)
        self.assertIn("go: audit snapshot is missing", pending.body)

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
        client = FakeGitHub()
        client.tracking = {"number": 42, "body": AUDIT_MARKER}
        pending = render_audit_report(AuditReportFixture.complete())

        deliver_audit_report(client, pending)

        self.assertEqual(client.updated[0][0], 42)
        self.assertIn(AUDIT_MARKER, client.updated[0][2] or "")


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
