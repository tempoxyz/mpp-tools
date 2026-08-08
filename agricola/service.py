from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from .commands import (
    AuthorizationError,
    CommandError,
    has_command_line,
    parse_commands,
    require_maintainer,
    resolve_labels,
)
from .github import GitHubError
from .ledger import CursorStore, DecisionLedger
from .models import (
    CanonicalChange,
    CommandVerb,
    Cursor,
    Decision,
    DecisionKind,
    LabelEvent,
    Manifest,
    PendingReply,
    PropagateDecision,
    PropagationRequest,
    PropagationResult,
    SkipDecision,
)
from .planner import build_tracking_issue, tracking_issue_title


class GitHub(Protocol):
    def merged_changes(self, repo: str, cursor: Cursor) -> list[CanonicalChange]: ...
    def pull_request(self, repo: str, number: int) -> CanonicalChange: ...
    def label_events(self, repo: str, number: int) -> Sequence[LabelEvent]: ...
    def find_tracking_issue(self, marker: str) -> dict[str, object] | None: ...
    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> dict[str, object]: ...
    def update_issue(
        self, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, object]: ...
    def source_from_body(self, body: str) -> tuple[str, int]: ...
    def pull_status(self, reference: str) -> str: ...


@dataclass(frozen=True)
class PollResult:
    discovered: int = 0
    created: int = 0
    deduplicated: int = 0
    suppressed: int = 0
    propagations: tuple[PropagationRequest, ...] = ()


def poll(
    client: GitHub,
    manifest: Manifest,
    ledger: DecisionLedger,
    cursor_store: CursorStore,
) -> PollResult:
    cursor = cursor_store.load()
    counters = {"discovered": 0, "created": 0, "deduplicated": 0, "suppressed": 0}
    propagations: list[PropagationRequest] = []
    for summary in client.merged_changes(manifest.canonical.repo, cursor):
        counters["discovered"] += 1
        change = client.pull_request(summary.repo, summary.number)
        events = client.label_events(change.repo, change.number)
        resolution = resolve_labels(events, change.merged_at, manifest)
        if any(
            label.lower().startswith("agricola:") for label in change.labels
        ) and not any(event.label.lower().startswith("agricola:") for event in events):
            resolution = replace(
                resolution,
                errors=(*resolution.errors, "could not verify merge-time label actors"),
            )
        existing_entry = ledger.read(summary.repo, summary.number)
        ledger.ensure(change, labels=resolution.labels)
        issue = client.find_tracking_issue(change.marker)
        if resolution.disabled and not resolution.errors and not resolution.notes:
            counters["suppressed"] += 1
        elif issue:
            counters["deduplicated"] += 1
        else:
            body = build_tracking_issue(change, resolution, manifest)
            issue = client.create_issue(tracking_issue_title(change), body)
            counters["created"] += 1
        if issue and not resolution.disabled and not resolution.errors:
            entry = ledger.read(change.repo, change.number)
            assert entry is not None
            propagations.extend(
                _requests_for_targets(
                    change,
                    resolution.targets,
                    dict(resolution.target_actors),
                    manifest,
                    issue,
                    build_tracking_issue(change, resolution, manifest),
                    entry.decisions,
                )
            )
        if existing_entry is not None and not issue:
            counters["deduplicated"] += 1
        cursor = cursor.advance(change)
        cursor_store.save(cursor)
    return PollResult(**counters, propagations=tuple(propagations))


@dataclass(frozen=True)
class CommentResult:
    commands: int = 0
    changed_ledger: bool = False
    ignored: bool = False
    reply: PendingReply | None = None
    propagations: tuple[PropagationRequest, ...] = ()


def handle_comment(
    client: GitHub,
    manifest: Manifest,
    ledger: DecisionLedger,
    event: dict[str, object],
) -> CommentResult:
    comment = _object(event, "comment")
    issue = _object(event, "issue")
    author = str(_object(comment, "user").get("login", ""))
    body = str(comment.get("body") or "")
    if not has_command_line(body):
        return CommentResult(ignored=True)
    try:
        require_maintainer(author, manifest)
    except AuthorizationError:
        return CommentResult(ignored=True)
    try:
        commands = parse_commands(body, manifest)
    except CommandError as exc:
        return CommentResult(
            reply=PendingReply(
                issue_number=int(str(issue["number"])),
                body=f"Agricola command error: {exc}",
            )
        )
    if not commands:
        return CommentResult(ignored=True)

    issue_number = int(str(issue["number"]))
    issue_body = str(issue.get("body") or "")
    try:
        source_repo, source_number = client.source_from_body(issue_body)
    except (GitHubError, ValueError):
        return CommentResult(
            commands=len(commands),
            reply=PendingReply(
                issue_number=issue_number,
                body=(
                    "Agricola commands require a tracking issue with a canonical "
                    "source marker."
                ),
            ),
        )
    change = client.pull_request(source_repo, source_number)
    changed_ledger = False
    replies: list[str] = []
    propagations: list[PropagationRequest] = []
    queued_targets: set[str] = set()
    for command in commands:
        if command.verb is CommandVerb.PLAN:
            events = client.label_events(change.repo, change.number)
            labels = resolve_labels(events, change.merged_at, manifest)
            client.update_issue(
                issue_number,
                title=tracking_issue_title(change),
                body=build_tracking_issue(change, labels, manifest),
            )
            replies.append(f"Regenerated the impact plan for `{change.source_id}`.")
        elif command.verb is CommandVerb.PROPAGATE:
            created = ledger.ensure(change)
            changed_ledger = changed_ledger or created
            targets = manifest.pr_targets() if command.all_targets else command.targets
            entry = ledger.read(change.repo, change.number)
            assert entry is not None
            recorded_targets = {decision.target for decision in entry.decisions}
            fresh_targets = tuple(
                target
                for target in targets
                if target not in recorded_targets and target not in queued_targets
            )
            requested = _requests_for_targets(
                change,
                fresh_targets,
                {target: author for target in targets},
                manifest,
                {
                    "number": issue_number,
                    "html_url": str(issue.get("html_url") or "https://github.com"),
                },
                issue_body,
                entry.decisions,
            )
            propagations.extend(requested)
            queued_targets.update(request.target for request in requested)
            for target in targets:
                if target in recorded_targets:
                    replies.append(f"Propagation for `{target}` is already recorded.")
                elif target in fresh_targets:
                    replies.append(f"Queued propagation for `{target}`.")
                else:
                    replies.append(f"Propagation for `{target}` is already queued.")
        elif command.verb is CommandVerb.SKIP:
            if command.target is None or command.reason is None:
                raise CommandError("invalid skip command")
            if ledger.read(change.repo, change.number) is None:
                events = client.label_events(change.repo, change.number)
                labels = resolve_labels(events, change.merged_at, manifest)
                ledger.ensure(change, labels=labels.labels)
            comment_id = comment.get("id") or event.get("delivery_id") or "unknown"
            decision = SkipDecision(
                target=command.target,
                decision=DecisionKind.SKIP,
                by=author,
                reason=command.reason,
                idempotency_key=f"issue-comment:{comment_id}:line:{command.line}",
                at=_event_time(comment),
            )
            appended = ledger.append(change, decision)
            changed_ledger = changed_ledger or appended
            action = "Recorded" if appended else "Already recorded"
            replies.append(f'{action} skip for `{command.target}`: "{command.reason}".')
        elif command.verb is CommandVerb.STATUS:
            entry = ledger.read(change.repo, change.number)
            references = (
                []
                if entry is None
                else [
                    decision.pr
                    for decision in entry.decisions
                    if decision.pr is not None
                ]
            )
            if references:
                statuses = "\n".join(
                    f"- {client.pull_status(reference)}" for reference in references
                )
                replies.append("Live downstream status:\n" + statuses)
            else:
                replies.append(
                    "No downstream pull requests are recorded for this change."
                )
    reply = (
        PendingReply(issue_number=issue_number, body="\n\n".join(replies))
        if replies
        else None
    )
    return CommentResult(
        len(commands),
        changed_ledger,
        reply=reply,
        propagations=tuple(propagations),
    )


def record_propagations(
    ledger: DecisionLedger, results: Sequence[PropagationResult]
) -> tuple[bool, tuple[PendingReply, ...]]:
    changed = False
    replies: dict[int, list[str]] = {}
    for result in results:
        request = result.request
        appended = ledger.append_source(
            request.source,
            PropagateDecision(
                target=request.target,
                decision=DecisionKind.PROPAGATE,
                by=request.by,
                pr=result.pr,
                idempotency_key=request.idempotency_key,
                at=result.at,
            ),
        )
        changed = changed or appended
        action = "Opened" if appended else "Already recorded"
        replies.setdefault(request.tracking_issue, []).append(
            f"{action} draft PR for `{request.target}`: [{result.pr}]({result.url})."
        )
    pending = tuple(
        PendingReply(issue_number=issue, body="\n".join(messages))
        for issue, messages in sorted(replies.items())
    )
    return changed, pending


def _requests_for_targets(
    change: CanonicalChange,
    targets: Sequence[str],
    actors: dict[str, str],
    manifest: Manifest,
    issue: dict[str, object],
    plan: str,
    decisions: Sequence[Decision],
) -> tuple[PropagationRequest, ...]:
    decided = {decision.target for decision in decisions}
    issue_number = int(str(issue["number"]))
    issue_url = str(issue.get("html_url") or issue.get("url") or "")
    requests: list[PropagationRequest] = []
    for target in targets:
        if target in decided:
            continue
        sdk = manifest.target(target)
        requests.append(
            PropagationRequest(
                source={
                    "repo": change.repo,
                    "pr": change.number,
                    "sha": change.sha,
                },
                source_title=change.title,
                source_url=change.url,
                target=target,
                target_repo=sdk.repo,
                tracking_issue=issue_number,
                tracking_issue_url=issue_url,
                by=actors[target],
                idempotency_key=f"propagate:{change.source_id}:{target}",
                branch=f"agricola/{change.source_id.replace('#', '-')}",
                verify=sdk.verify,
                changelog=sdk.changelog,
                owners=sdk.owners,
                plan=plan,
            )
        )
    return tuple(requests)


def _object(value: dict[str, object], key: str) -> dict[str, object]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise TypeError(f"event.{key} must be an object")
    return result


def _event_time(comment: dict[str, object]) -> datetime:
    raw = comment.get("created_at")
    if not raw:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(raw)).astimezone(UTC)
