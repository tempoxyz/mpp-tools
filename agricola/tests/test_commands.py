from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from agricola.commands import (
    AuthorizationError,
    CommandError,
    parse_commands,
    require_maintainer,
    resolve_labels,
)
from agricola.models import CommandVerb, LabelAction, LabelEvent
from agricola.tests.helpers import manifest


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = manifest()

    def test_parses_only_first_token_commands(self) -> None:
        commands = parse_commands(
            "please run @agricola plan\n  @agricola plan", self.manifest
        )
        self.assertEqual([command.verb for command in commands], [CommandVerb.PLAN])
        self.assertEqual(commands[0].line, 2)

    def test_parses_quoted_skip_reason(self) -> None:
        command = parse_commands(
            '@agricola skip ruby reason="TS-only tooling"', self.manifest
        )[0]
        self.assertEqual(command.target, "ruby")
        self.assertEqual(command.reason, "TS-only tooling")

    def test_rejects_unsupported_command(self) -> None:
        with self.assertRaisesRegex(CommandError, "unknown command"):
            parse_commands("@agricola destroy everything", self.manifest)

    def test_parses_propagation_targets(self) -> None:
        command = parse_commands("@agricola propagate go rust go", self.manifest)[0]
        self.assertEqual(command.verb, CommandVerb.PROPAGATE)
        self.assertEqual(command.targets, ("go", "rust"))

    def test_parses_all_propagation(self) -> None:
        command = parse_commands("@agricola propagate All", self.manifest)[0]
        self.assertTrue(command.all_targets)

    def test_rejects_notify_only_propagation(self) -> None:
        with self.assertRaisesRegex(CommandError, "does not support PR automation"):
            parse_commands("@agricola propagate ruby", self.manifest)

    def test_rejects_mixed_all_propagation(self) -> None:
        with self.assertRaisesRegex(CommandError, "cannot be combined"):
            parse_commands("@agricola propagate all go", self.manifest)

    def test_rejects_propagating_and_skipping_the_same_target(self) -> None:
        with self.assertRaisesRegex(CommandError, "both propagated and skipped: go"):
            parse_commands(
                '@agricola propagate all\n@agricola skip go reason="not needed"',
                self.manifest,
            )

    def test_rejects_unknown_target(self) -> None:
        with self.assertRaisesRegex(CommandError, "unknown SDK target"):
            parse_commands('@agricola skip golang reason="wrong"', self.manifest)

    def test_requires_skip_reason(self) -> None:
        with self.assertRaisesRegex(CommandError, "requires reason"):
            parse_commands("@agricola skip go", self.manifest)

    def test_authorization_is_case_insensitive(self) -> None:
        require_maintainer("BrendanJRyan", self.manifest)
        with self.assertRaises(AuthorizationError):
            require_maintainer("outsider", self.manifest)


class LabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = manifest()
        self.merged = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)

    def event(
        self,
        label: str,
        *,
        actor: str = "maintainer",
        action: LabelAction = LabelAction.LABELED,
        offset: int = -1,
    ) -> LabelEvent:
        return LabelEvent(action, label, actor, self.merged + timedelta(minutes=offset))

    def test_additive_targets(self) -> None:
        labels = ["agricola:go", "agricola:rust"]
        result = resolve_labels(
            [self.event(label) for label in labels], self.merged, self.manifest
        )
        self.assertEqual(result.targets, ("go", "rust"))

    def test_all_expands_only_pr_targets(self) -> None:
        result = resolve_labels(
            [self.event("agricola:all")], self.merged, self.manifest
        )
        self.assertEqual(result.targets, ("go", "rust"))
        self.assertEqual(
            result.target_actors,
            (("go", "maintainer"), ("rust", "maintainer")),
        )

    def test_notify_only_label_does_not_queue_a_pull_request(self) -> None:
        result = resolve_labels(
            [self.event("agricola:ruby")], self.merged, self.manifest
        )

        self.assertEqual(result.targets, ())
        self.assertEqual(result.target_actors, ())
        self.assertEqual(
            result.notes,
            ("ruby is notify-only; no downstream PR was queued",),
        )

    def test_none_wins_and_reports_conflict(self) -> None:
        labels = ["agricola:none", "agricola:go"]
        result = resolve_labels(
            [self.event(label) for label in labels], self.merged, self.manifest
        )
        self.assertTrue(result.disabled)
        self.assertEqual(result.targets, ())
        self.assertIn("overrides", result.notes[0])

    def test_unknown_label_is_error(self) -> None:
        result = resolve_labels(
            [self.event("agricola:golang")],
            self.merged,
            self.manifest,
        )
        self.assertEqual(result.errors, ("unknown label: agricola:golang",))

    def test_unauthorized_and_post_merge_labels_do_not_count(self) -> None:
        result = resolve_labels(
            [
                self.event("agricola:go", actor="outsider"),
                self.event("agricola:rust", offset=1),
            ],
            self.merged,
            self.manifest,
        )
        self.assertEqual(result.labels, ())
        self.assertEqual(result.targets, ())

    def test_latest_label_application_must_be_authorized(self) -> None:
        events = [
            self.event("agricola:go", offset=-3),
            self.event(
                "agricola:go",
                actor="outsider",
                action=LabelAction.UNLABELED,
                offset=-2,
            ),
            self.event("agricola:go", actor="outsider", offset=-1),
        ]
        result = resolve_labels(events, self.merged, self.manifest)
        self.assertEqual(result.targets, ())

    def test_post_merge_removal_does_not_change_snapshot(self) -> None:
        events = [
            self.event("agricola:go"),
            self.event(
                "agricola:go",
                action=LabelAction.UNLABELED,
                offset=1,
            ),
        ]
        result = resolve_labels(events, self.merged, self.manifest)
        self.assertEqual(result.labels, ("agricola:go",))
        self.assertEqual(result.targets, ("go",))


if __name__ == "__main__":
    unittest.main()
