import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]


class AgricolaWorkflowTests(unittest.TestCase):
    def test_concurrency_scopes_commands_to_tracking_issue(self) -> None:
        workflow = yaml.load(
            (ROOT / ".github/workflows/agricola.yml").read_text(),
            Loader=yaml.BaseLoader,
        )

        group = workflow["concurrency"]["group"]
        command_filter = "contains(github.event.comment.body, '/ag')"
        self.assertIn(command_filter, group)
        self.assertIn(command_filter, workflow["jobs"]["run"]["if"])
        self.assertIn("format('fix-{0}', github.event.issue.number)", group)
        self.assertIn("format('ignored-{0}', github.event.comment.id)", group)
        self.assertIn("|| 'control-plane'", group)
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")


if __name__ == "__main__":
    unittest.main()
