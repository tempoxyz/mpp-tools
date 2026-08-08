from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from datetime import datetime

from .models import (
    Automation,
    Command,
    CommandVerb,
    LabelAction,
    LabelEvent,
    LabelResolution,
    Manifest,
)

_COMMAND_PREFIX = re.compile(r"^\s*@agricola(?:\s+|$)", re.IGNORECASE)


class CommandError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


def has_command_line(body: str) -> bool:
    return any(_COMMAND_PREFIX.match(line) for line in body.splitlines())


def require_maintainer(author: str, manifest: Manifest) -> None:
    if author.lower() not in {login.lower() for login in manifest.maintainers}:
        raise AuthorizationError(f"@{author} is not authorized to operate Agricola")


def parse_commands(body: str, manifest: Manifest) -> list[Command]:
    commands: list[Command] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        if not _COMMAND_PREFIX.match(line):
            continue
        try:
            tokens = shlex.split(line.strip())
        except ValueError as exc:
            raise CommandError(f"line {line_number}: {exc}") from exc
        if len(tokens) < 2:
            raise CommandError(f"line {line_number}: missing command verb")
        verb_text = tokens[1].lower()
        try:
            verb = CommandVerb(verb_text)
        except ValueError as exc:
            raise CommandError(
                f"line {line_number}: unknown command {verb_text!r}"
            ) from exc
        args = tokens[2:]
        if verb in {CommandVerb.PLAN, CommandVerb.STATUS}:
            if args:
                raise CommandError(
                    f"line {line_number}: {verb.value} takes no arguments"
                )
            commands.append(Command(verb=verb, line=line_number))
            continue

        if verb is CommandVerb.PROPAGATE:
            if not args:
                raise CommandError(
                    f"line {line_number}: propagate requires SDK targets or all"
                )
            if len(args) == 1 and args[0].lower() == "all":
                commands.append(Command(verb=verb, all_targets=True, line=line_number))
                continue
            if any(argument.lower() == "all" for argument in args):
                raise CommandError(
                    f"line {line_number}: all cannot be combined with SDK targets"
                )
            targets: list[str] = []
            for argument in args:
                target = argument.lower()
                try:
                    sdk = manifest.target(target)
                except ValueError as exc:
                    raise CommandError(f"line {line_number}: {exc}") from exc
                if sdk.automation is not Automation.PR:
                    raise CommandError(
                        f"line {line_number}: {target} does not support PR automation"
                    )
                if target not in targets:
                    targets.append(target)
            commands.append(
                Command(verb=verb, targets=tuple(targets), line=line_number)
            )
            continue

        if not args:
            raise CommandError(f"line {line_number}: skip requires an SDK target")
        target = args[0].lower()
        try:
            manifest.target(target)
        except ValueError as exc:
            raise CommandError(f"line {line_number}: {exc}") from exc
        reason: str | None = None
        for argument in args[1:]:
            if argument.startswith("reason="):
                if reason is not None:
                    raise CommandError(
                        f"line {line_number}: reason specified more than once"
                    )
                reason = argument.removeprefix("reason=").strip()
            else:
                raise CommandError(
                    f"line {line_number}: unexpected argument {argument!r}"
                )
        if not reason:
            raise CommandError(f'line {line_number}: skip requires reason="..."')
        commands.append(
            Command(verb=verb, target=target, reason=reason, line=line_number)
        )
    propagation_targets = {
        target
        for command in commands
        if command.verb is CommandVerb.PROPAGATE
        for target in (
            manifest.pr_targets() if command.all_targets else command.targets
        )
    }
    skipped_targets = {
        command.target
        for command in commands
        if command.verb is CommandVerb.SKIP and command.target is not None
    }
    conflicts = sorted(propagation_targets & skipped_targets)
    if conflicts:
        raise CommandError(
            "targets cannot be both propagated and skipped: " + ", ".join(conflicts)
        )
    return commands


def resolve_labels(
    events: Iterable[LabelEvent],
    merged_at: datetime,
    manifest: Manifest,
) -> LabelResolution:
    authorized = {login.lower() for login in manifest.maintainers}
    last_events: dict[str, LabelEvent] = {}
    for event in sorted(events, key=lambda item: item.created_at):
        label = event.label.lower()
        if not label.startswith("agricola:") or event.created_at > merged_at:
            continue
        if event.action in {LabelAction.LABELED, LabelAction.UNLABELED}:
            last_events[label] = event

    applied = {
        label: event
        for label, event in last_events.items()
        if event.action is LabelAction.LABELED and event.actor.lower() in authorized
    }

    known = {
        "agricola:all",
        "agricola:none",
        *(f"agricola:{name}" for name in manifest.sdks),
    }
    unknown = sorted(set(applied) - known)
    errors = tuple(f"unknown label: {label}" for label in unknown)
    effective = set(applied) & known
    if "agricola:none" in effective:
        conflicts = sorted(effective - {"agricola:none"})
        notes = ()
        if conflicts:
            notes = ("agricola:none overrides " + ", ".join(conflicts),)
        return LabelResolution(
            labels=tuple(sorted(applied)),
            targets=(),
            disabled=True,
            errors=errors,
            notes=notes,
        )

    target_actors = {
        label.removeprefix("agricola:"): applied[label].actor
        for label in effective
        if label != "agricola:all"
        and manifest.target(label.removeprefix("agricola:")).automation is Automation.PR
    }
    notify_targets = sorted(
        label.removeprefix("agricola:")
        for label in effective
        if label != "agricola:all"
        and manifest.target(label.removeprefix("agricola:")).automation
        is Automation.NOTIFY
    )
    if "agricola:all" in effective:
        actor = applied["agricola:all"].actor
        for name, sdk in manifest.sdks.items():
            if sdk.automation is Automation.PR:
                target_actors.setdefault(name, actor)
    return LabelResolution(
        labels=tuple(sorted(applied)),
        targets=tuple(sorted(target_actors)),
        target_actors=tuple(sorted(target_actors.items())),
        errors=errors,
        notes=tuple(
            f"{target} is notify-only; no downstream PR was queued"
            for target in notify_targets
        ),
    )
