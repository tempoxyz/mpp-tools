from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from agricola.executor import (
    VerificationError,
    pull_request_body,
    pull_request_title,
    verify,
)
from agricola.models import (
    AuditSource,
    PropagationRequest,
    PropagationResult,
    PropagationSkip,
)


def request() -> PropagationRequest:
    return PropagationRequest(
        source={"repo": "wevm/mppx", "pr": 412, "sha": "abc1234567"},
        source_title="Add refunds",
        source_url="https://github.com/wevm/mppx/pull/412",
        target="go",
        target_repo="tempoxyz/mpp-go",
        target_base_sha="def4567890",
        tracking_issue=207,
        tracking_issue_url="https://github.com/tempoxyz/mpp-tools/issues/207",
        by="maintainer",
        idempotency_key="propagate:mppx#412:go",
        branch="agricola/mppx-412",
        verify=("make test", "make conformance"),
        changelog="keep-a-changelog",
        plan="Plan",
    )


def audit_request() -> PropagationRequest:
    return request().model_copy(
        update={
            "source": AuditSource(
                repo="wevm/mppx",
                sha="abc1234567",
                finding="AGR-2026-022",
                fingerprint="semantic:challenge/select-intent",
            ),
            "source_title": "fix: select a supported challenge intent",
            "source_url": "https://github.com/tempoxyz/mpp-tools/issues/97",
            "tracking_issue": 97,
            "tracking_issue_url": "https://github.com/tempoxyz/mpp-tools/issues/97",
            "branch": "agricola/agr-2026-022",
        }
    )


class ExecutorTests(unittest.TestCase):
    def test_renders_stable_draft_pr_metadata(self) -> None:
        title = pull_request_title(request())
        body = pull_request_body(request())

        self.assertEqual(title, "Add refunds")
        self.assertIn("## Motivation", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Key design considerations", body)
        self.assertIn("agricola:source=wevm/mppx#412 target=go", body)
        self.assertNotIn("## Testing", body)

    def test_renders_audit_finding_pr_metadata(self) -> None:
        body = pull_request_body(audit_request())

        self.assertIn("agricola:audit-finding=AGR-2026-022 target=go", body)
        self.assertIn("Resolve [AGR-2026-022]", body)
        self.assertIn("tracking issue #97", body)

    @patch("agricola.executor.subprocess.run")
    def test_runs_manifest_commands_in_order(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0)

        verify(request(), "/target")

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["bash", "-lc", "make test"], ["bash", "-lc", "make conformance"]],
        )
        self.assertTrue(
            all(call.kwargs["cwd"] == "/target" for call in run.call_args_list)
        )

    @patch("agricola.executor.subprocess.run")
    def test_stops_after_failed_verification(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 2)

        with self.assertRaisesRegex(VerificationError, "make test"):
            verify(request())

        self.assertEqual(run.call_count, 1)

    def test_result_pull_request_must_belong_to_target_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "pr must belong"):
            PropagationResult(
                request=request(),
                pr="tempoxyz/pympp#88",
                url="https://github.com/tempoxyz/pympp/pull/88",
            )

    def test_skip_requires_a_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1 character"):
            PropagationSkip(request=request(), reason="")


if __name__ == "__main__":
    unittest.main()
