import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]


class AgricolaWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.control_text = (ROOT / ".github/workflows/agricola.yml").read_text()
        self.audit_text = (ROOT / ".github/workflows/agricola-audit.yml").read_text()
        self.ci_text = (ROOT / ".github/workflows/ci.yml").read_text()
        self.state_action_text = (
            ROOT / ".github/actions/persist-agricola-state/action.yml"
        ).read_text()
        self.control = yaml.load(self.control_text, Loader=yaml.BaseLoader)
        self.audit = yaml.load(self.audit_text, Loader=yaml.BaseLoader)

    def test_concurrency_scopes_commands_to_tracking_issue(self) -> None:
        group = self.control["concurrency"]["group"]
        command_filter = "contains(github.event.comment.body, '/ag')"
        self.assertIn(command_filter, group)
        self.assertIn(command_filter, self.control["jobs"]["run"]["if"])
        self.assertIn("format('fix-{0}', github.event.issue.number)", group)
        self.assertIn("format('ignored-{0}', github.event.comment.id)", group)
        self.assertIn("|| 'control-plane'", group)
        self.assertEqual(self.control["concurrency"]["cancel-in-progress"], "false")

    def test_state_writers_use_replayable_git_transactions(self) -> None:
        writers = (
            self.control["jobs"]["run"],
            self.control["jobs"]["record"],
            self.audit["jobs"]["report"],
        )

        self.assertTrue(all("concurrency" not in job for job in writers))
        self.assertEqual(self.control_text.count("agricola state-transaction"), 2)
        self.assertEqual(self.audit_text.count("agricola state-transaction"), 1)
        self.assertNotIn("agricola-state-writer", self.control_text + self.audit_text)
        self.assertNotIn("create-pull-request", self.state_action_text)
        self.assertIn("gh pr create", self.state_action_text)

    def test_target_tokens_are_manifest_derived_and_matrix_scoped(self) -> None:
        self.assertNotIn("mpp-rs", self.control_text)
        self.assertNotIn("pympp", self.control_text)
        self.assertIn("agricola token-scope", self.control_text)
        self.assertIn("owner: ${{ matrix.owner }}", self.control_text)
        self.assertIn("repositories: ${{ matrix.repository }}", self.control_text)

    def test_recurring_audit_compares_each_sdk_to_pinned_canonical(self) -> None:
        target = self.audit["jobs"]["target"]
        self.assertEqual(self.audit["on"]["schedule"][0]["cron"], "0 9 * * 1")
        self.assertIn("agricola audit-matrix", self.audit_text)
        self.assertEqual(
            target["strategy"]["matrix"],
            "${{ fromJSON(needs.prepare.outputs.matrix) }}",
        )
        self.assertIn("repository: ${{ matrix.repo }}", self.audit_text)
        self.assertIn(
            "repository: ${{ needs.prepare.outputs.canonical-repo }}",
            self.audit_text,
        )
        self.assertIn(
            "ref: ${{ needs.prepare.outputs.canonical-sha }}", self.audit_text
        )
        self.assertIn("Compare SDK implementation to canonical mppx", self.audit_text)

    def test_publication_logic_runs_through_tested_cli(self) -> None:
        self.assertIn("agricola publish-propagation", self.control_text)
        self.assertNotIn("gh pr create", self.control_text)
        self.assertNotIn("git merge-base --is-ancestor", self.control_text)

    def test_ci_enforces_locked_quality_checks(self) -> None:
        for command in (
            "uv sync --locked --group dev",
            "ruff format --check agricola",
            "ruff check agricola",
            "ty check agricola",
            "coverage report",
            "actionlint@v1.7.7",
        ):
            self.assertIn(command, self.ci_text)


if __name__ == "__main__":
    unittest.main()
