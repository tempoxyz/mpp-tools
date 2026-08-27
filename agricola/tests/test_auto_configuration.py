import unittest
from pathlib import Path
from typing import Any, cast

import yaml


ROOT = Path(__file__).parents[2]
AUTO = ROOT / ".auto"


def load_agent(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((AUTO / "agents" / f"{name}.yaml").read_text()),
    )


def mount(agent: dict[str, Any], name: str) -> dict[str, Any]:
    mounts = cast(list[dict[str, Any]], agent["mounts"])
    return next(item for item in mounts if item["name"] == name)


class AutoConfigurationTests(unittest.TestCase):
    def test_defines_only_scout_and_implementer_agents(self) -> None:
        agents = sorted(path.stem for path in (AUTO / "agents").glob("*.yaml"))
        self.assertEqual(agents, ["agricola-implementer", "agricola-scout"])

    def test_agents_share_the_reviewed_runtime(self) -> None:
        expected_import = "../fragments/environments/agricola-runtime.yaml"
        for name in ("agricola-scout", "agricola-implementer"):
            with self.subTest(name=name):
                agent = load_agent(name)
                self.assertEqual(agent["imports"], [expected_import])
                prompt = agent["systemPrompt"]["file"]
                self.assertTrue((AUTO / "agents" / prompt).resolve().is_file())

    def test_scout_cannot_write_downstream_repositories(self) -> None:
        scout = load_agent("agricola-scout")
        for name in ("rust", "python"):
            with self.subTest(name=name):
                capabilities = mount(scout, name)["auth"]["capabilities"]
                self.assertEqual(capabilities["contents"], "read")
                self.assertEqual(capabilities["pullRequests"], "read")
                self.assertEqual(capabilities["merge"], "none")

    def test_implementer_is_draft_only_and_target_scoped(self) -> None:
        implementer = load_agent("agricola-implementer")
        repositories = {item["repository"] for item in implementer["mounts"]}
        self.assertNotIn("tempoxyz/mpp-go", repositories)
        self.assertNotIn("stripe/mpp-rb", repositories)
        self.assertNotIn("stripe/mpp-java", repositories)
        for name in ("rust", "python"):
            with self.subTest(name=name):
                capabilities = mount(implementer, name)["auth"]["capabilities"]
                self.assertEqual(capabilities["contents"], "write")
                self.assertEqual(capabilities["pullRequests"], "write")
                self.assertEqual(capabilities["merge"], "none")
                self.assertEqual(capabilities["secrets"], "none")

    def test_approval_and_feedback_use_native_github_events(self) -> None:
        implementer = load_agent("agricola-implementer")
        triggers = {item["name"]: item for item in implementer["triggers"]}
        approval = triggers["proposal-approved"]
        self.assertEqual(approval["event"], "github.issue.labeled")
        self.assertEqual(approval["where"]["$.github.label.name"], "agricola:approved")
        self.assertEqual(approval["routing"]["bind"]["target"], "github.issue")
        self.assertEqual(
            triggers["proposal-feedback"]["event"],
            "github.issue.comment.created",
        )
        self.assertEqual(
            triggers["pull-request-feedback"]["routing"]["target"],
            "github.pull_request",
        )

    def test_scout_runs_continuously_and_weekly(self) -> None:
        scout = load_agent("agricola-scout")
        triggers = {item["name"]: item for item in scout["triggers"]}
        self.assertEqual(triggers["continuous-scan"]["cron"], "*/30 * * * *")
        self.assertEqual(triggers["weekly-audit"]["cron"], "0 9 * * 1")


if __name__ == "__main__":
    unittest.main()
