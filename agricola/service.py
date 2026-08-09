from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from .audit import (
    AuditError,
    audit_finding_context_from_body,
    ensure_audit_remediation,
)
from .commands import (
    AuthorizationError,
    CommandError,
    has_command_line,
    parse_commands,
    require_maintainer,
    resolve_labels,
)
from .github import GitHubError, agricola_issue_labels
from .ledger import CursorStore, DecisionLedger
from .models import (
    AuditFindingContext,
    AuditSource,
    Automation,
    CanonicalChange,
    Command,
    CommandVerb,
    Cursor,
    Decision,
    DecisionKind,
    LabelEvent,
    Manifest,
    PendingIssueUpdate,
    PendingReply,
    PropagateDecision,
    PropagationOutcome,
    PropagationRequest,
    PropagationRevision,
    PropagationSkip,
    Source,
    SkipDecision,
)
from .planner import (
    build_tracking_issue,
    queued_propagations,
    recorded_propagations,
    record_propagation_outcomes,
    tracking_issue_title,
)


class GitHub(Protocol):
    def merged_changes(self, repo: str, cursor: Cursor) -> list[CanonicalChange]: ...
    def pull_request(self, repo: str, number: int) -> CanonicalChange: ...
    def repository_head(self, repo: str) -> str: ...
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
    def pull_revision(
        self, reference: str, expected_branch: str
    ) -> PropagationRevision: ...


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
        entry = ledger.read(change.repo, change.number)
        assert entry is not None
        plan = build_tracking_issue(
            change,
            resolution,
            manifest,
            decisions=entry.decisions,
        )
        issue = client.find_tracking_issue(change.marker)
        if resolution.disabled and not resolution.errors and not resolution.notes:
            counters["suppressed"] += 1
        elif issue:
            counters["deduplicated"] += 1
        else:
            issue = client.create_issue(
                tracking_issue_title(change),
                plan,
                agricola_issue_labels(resolution.affected),
            )
            counters["created"] += 1
        if issue and not resolution.disabled and not resolution.errors:
            propagations.extend(
                _requests_for_targets(
                    client,
                    change,
                    resolution.targets,
                    dict(resolution.target_actors),
                    manifest,
                    issue,
                    plan,
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
    issue_update: PendingIssueUpdate | None = None
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
        audit_context = audit_finding_context_from_body(issue_body)
    except AuditError as exc:
        return CommentResult(
            commands=len(commands),
            reply=PendingReply(
                issue_number=issue_number,
                body=f"Agricola command error: {exc}",
            ),
        )
    if audit_context is not None:
        return _handle_audit_comment(
            client,
            manifest,
            issue,
            author,
            commands,
            audit_context,
        )
    try:
        source_repo, source_number = client.source_from_body(issue_body)
        if source_repo.lower() != manifest.canonical.repo.lower():
            raise ValueError("source repository is not canonical")
        change = client.pull_request(source_repo, source_number)
    except (GitHubError, ValueError):
        return CommentResult(
            commands=len(commands),
            reply=PendingReply(
                issue_number=issue_number,
                body=(
                    "Agricola commands require a tracking issue tied to a merged "
                    "canonical pull request."
                ),
            ),
        )
    changed_ledger = False
    replies: list[str] = []
    propagations: list[PropagationRequest] = []
    queued_targets: set[str] = set()
    scheduled_targets: set[str] = set()
    comment_id = str(comment.get("id") or event.get("delivery_id") or "unknown")
    for command in commands:
        if command.verb is CommandVerb.PLAN:
            replies.append(f"Regenerated the impact plan for `{change.source_id}`.")
        elif command.verb is CommandVerb.PROPAGATE:
            created = ledger.ensure(change)
            changed_ledger = changed_ledger or created
            targets = manifest.pr_targets() if command.all_targets else command.targets
            entry = ledger.read(change.repo, change.number)
            assert entry is not None
            recorded_targets = {decision.target for decision in entry.decisions}
            recorded_prs = {
                decision.target: decision.pr
                for decision in entry.decisions
                if decision.pr is not None
            }
            for target in targets:
                if target in scheduled_targets:
                    replies.append(f"Fix for `{target}` is already queued.")
                    continue
                if target in recorded_targets:
                    reference = recorded_prs.get(target)
                    if command.instruction is None:
                        replies.append(f"Fix for `{target}` is already recorded.")
                        continue
                    if reference is None:
                        replies.append(
                            f"Cannot revise `{target}` because no pull request was recorded."
                        )
                        continue
                    branch = f"agricola/{change.source_id.replace('#', '-')}"
                    try:
                        revision = client.pull_revision(reference, branch)
                    except GitHubError as exc:
                        replies.append(f"Cannot revise `{target}`: {exc}.")
                        continue
                    propagations.append(
                        _request_for_target(
                            client,
                            change,
                            target,
                            author,
                            manifest,
                            issue,
                            issue_body,
                            instruction=command.instruction,
                            revision=revision,
                            idempotency_key=(
                                f"revise:{change.source_id}:{target}:{comment_id}:"
                                f"{command.line}"
                            ),
                        )
                    )
                    scheduled_targets.add(target)
                    replies.append(
                        f"Queued revision of `{target}` using PR feedback and CI failures."
                    )
                    continue
                requested = _requests_for_targets(
                    client,
                    change,
                    (target,),
                    {target: author},
                    manifest,
                    {
                        "number": issue_number,
                        "html_url": str(issue.get("html_url") or "https://github.com"),
                    },
                    issue_body,
                    entry.decisions,
                )
                requested = tuple(
                    request.model_copy(
                        update={"instruction": command.instruction}, deep=True
                    )
                    for request in requested
                )
                propagations.extend(requested)
                if requested:
                    queued_targets.add(target)
                    scheduled_targets.add(target)
                    replies.append(f"Queued fix for `{target}`.")
                else:
                    replies.append(f"Fix for `{target}` is already queued.")
        elif command.verb is CommandVerb.SKIP:
            if command.target is None or command.reason is None:
                raise CommandError("invalid skip command")
            if ledger.read(change.repo, change.number) is None:
                events = client.label_events(change.repo, change.number)
                labels = resolve_labels(events, change.merged_at, manifest)
                ledger.ensure(change, labels=labels.labels)
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
    update_requested = any(
        command.verb in {CommandVerb.PLAN, CommandVerb.PROPAGATE, CommandVerb.SKIP}
        for command in commands
    )
    issue_update = None
    if update_requested:
        events = client.label_events(change.repo, change.number)
        labels = resolve_labels(events, change.merged_at, manifest)
        entry = ledger.read(change.repo, change.number)
        decisions = () if entry is None else entry.decisions
        updated_plan = build_tracking_issue(
            change,
            labels,
            manifest,
            decisions=decisions,
            queued_targets=queued_targets,
        )
        issue_update = PendingIssueUpdate(
            issue_number=issue_number,
            body=updated_plan,
        )
        propagations = [
            request.model_copy(update={"plan": updated_plan})
            for request in propagations
        ]
    reply = (
        PendingReply(issue_number=issue_number, body="\n\n".join(replies))
        if replies
        else None
    )
    return CommentResult(
        len(commands),
        changed_ledger,
        reply=reply,
        issue_update=issue_update,
        propagations=tuple(propagations),
    )


def record_propagations(
    ledger: DecisionLedger, results: Sequence[PropagationOutcome]
) -> tuple[
    bool,
    tuple[PendingReply, ...],
    tuple[PendingIssueUpdate, ...],
]:
    changed = False
    replies: dict[int, list[str]] = {}
    issue_results: dict[int, list[PropagationOutcome]] = {}
    for result in results:
        request = result.request
        if isinstance(result, PropagationSkip):
            decision: Decision = SkipDecision(
                target=request.target,
                decision=DecisionKind.SKIP,
                by=request.by,
                reason=result.reason,
                idempotency_key=request.idempotency_key,
                at=result.at,
            )
            message = f'Skipped `{request.target}`: "{result.reason}".'
        else:
            decision = PropagateDecision(
                target=request.target,
                decision=DecisionKind.PROPAGATE,
                by=request.by,
                pr=result.pr,
                idempotency_key=request.idempotency_key,
                at=result.at,
            )
            message = (
                f"Revised draft PR for `{request.target}`: [{result.pr}]({result.url})."
                if request.revision is not None
                else f"Opened draft PR for `{request.target}`: "
                f"[{result.pr}]({result.url})."
            )
        canonical = isinstance(request.source, Source)
        appended = (
            ledger.append_source(request.source, decision)
            if canonical and request.revision is None
            else False
        )
        changed = changed or appended
        if canonical and not appended and request.revision is None:
            message = f"Already recorded: {message[0].lower()}{message[1:]}"
        replies.setdefault(request.tracking_issue, []).append(message)
        issue_results.setdefault(request.tracking_issue, []).append(result)
    pending = tuple(
        PendingReply(issue_number=issue, body="\n".join(messages))
        for issue, messages in sorted(replies.items())
    )
    updates = tuple(
        PendingIssueUpdate(
            issue_number=issue,
            body=record_propagation_outcomes(grouped[0].request.plan, grouped),
        )
        for issue, grouped in sorted(issue_results.items())
    )
    return changed, pending, updates


def _handle_audit_comment(
    client: GitHub,
    manifest: Manifest,
    issue: dict[str, object],
    author: str,
    commands: Sequence[Command],
    context: AuditFindingContext,
) -> CommentResult:
    issue_number = int(str(issue["number"]))
    unsupported = tuple(
        command.verb.value
        for command in commands
        if command.verb not in {CommandVerb.PROPAGATE, CommandVerb.STATUS}
    )
    if unsupported:
        return CommentResult(
            commands=len(commands),
            reply=PendingReply(
                issue_number=issue_number,
                body=(
                    "Agricola command error: audit finding issues support only "
                    "`fix` and `status`."
                ),
            ),
        )

    for command in commands:
        if command.verb is not CommandVerb.PROPAGATE or command.all_targets:
            continue
        outside_finding = tuple(
            target for target in command.targets if target not in context.affected
        )
        if outside_finding:
            return CommentResult(
                commands=len(commands),
                reply=PendingReply(
                    issue_number=issue_number,
                    body=(
                        "Agricola command error: this finding does not affect: "
                        + ", ".join(outside_finding)
                    ),
                ),
            )

    issue_body = ensure_audit_remediation(
        str(issue.get("body") or ""), context, manifest
    )
    recorded = recorded_propagations(issue_body)
    initial_targets: list[str] = []
    requests: list[tuple[str, str | None, PropagationRevision | None]] = []
    scheduled_targets: set[str] = set()
    replies: list[str] = []
    for command in commands:
        if command.verb is CommandVerb.STATUS:
            if recorded:
                statuses = "\n".join(
                    f"- {client.pull_status(reference)}"
                    for reference in recorded.values()
                )
                replies.append("Live remediation status:\n" + statuses)
            else:
                replies.append(
                    "No remediation pull requests are recorded for this finding."
                )
            continue
        targets = (
            tuple(
                target
                for target in context.affected
                if manifest.target(target).automation is Automation.PR
            )
            if command.all_targets
            else command.targets
        )
        if not targets:
            replies.append("No affected SDK supports draft PR automation.")
        for target in targets:
            if target in scheduled_targets:
                replies.append(f"Remediation for `{target}` is already queued.")
                continue
            reference = recorded.get(target)
            if reference is not None:
                if command.instruction is None:
                    replies.append(f"Remediation for `{target}` is already recorded.")
                    continue
                branch = f"agricola/{context.id.lower()}"
                try:
                    revision = client.pull_revision(reference, branch)
                except GitHubError as exc:
                    replies.append(f"Cannot revise `{target}`: {exc}.")
                    continue
                requests.append((target, command.instruction, revision))
                scheduled_targets.add(target)
                replies.append(
                    f"Queued revision of `{target}` using PR feedback and CI failures."
                )
            else:
                initial_targets.append(target)
                requests.append((target, command.instruction, None))
                scheduled_targets.add(target)
                replies.append(f"Queued remediation for `{target}`.")

    updated_body = queued_propagations(issue_body, initial_targets)
    issue_url = str(issue.get("html_url") or issue.get("url") or "")
    title = str(issue.get("title") or context.id)
    summary = title.split(": ", 1)[-1]
    propagations = tuple(
        PropagationRequest(
            source=AuditSource(
                repo=context.canonical.repo,
                sha=context.canonical.sha,
                finding=context.id,
                fingerprint=context.fingerprint,
            ),
            source_title=f"fix: {summary}",
            source_url=issue_url,
            target=target,
            target_repo=context.affected[target].repo,
            target_base_sha=(
                revision.head_sha
                if revision is not None
                else context.affected[target].sha
            ),
            tracking_issue=issue_number,
            tracking_issue_url=issue_url,
            by=author,
            idempotency_key=(
                f"revise:audit:{context.id}:{target}:{revision.head_sha[:12]}"
                if revision is not None
                else f"audit:{context.id}:{target}"
            ),
            branch=f"agricola/{context.id.lower()}",
            verify=manifest.target(target).verify,
            changelog=manifest.target(target).changelog,
            owners=manifest.target(target).owners,
            plan=updated_body,
            instruction=instruction,
            revision=revision,
        )
        for target, instruction, revision in requests
    )
    update = (
        PendingIssueUpdate(issue_number=issue_number, body=updated_body)
        if initial_targets or updated_body != str(issue.get("body") or "")
        else None
    )
    return CommentResult(
        commands=len(commands),
        reply=(
            PendingReply(issue_number=issue_number, body="\n\n".join(replies))
            if replies
            else None
        ),
        issue_update=update,
        propagations=propagations,
    )


def _requests_for_targets(
    client: GitHub,
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
        requests.append(
            _request_for_target(
                client,
                change,
                target,
                actors[target],
                manifest,
                {"number": issue_number, "html_url": issue_url},
                plan,
            )
        )
    return tuple(requests)


def _request_for_target(
    client: GitHub,
    change: CanonicalChange,
    target: str,
    actor: str,
    manifest: Manifest,
    issue: dict[str, object],
    plan: str,
    *,
    instruction: str | None = None,
    revision: PropagationRevision | None = None,
    idempotency_key: str | None = None,
) -> PropagationRequest:
    sdk = manifest.target(target)
    return PropagationRequest(
        source={
            "repo": change.repo,
            "pr": change.number,
            "sha": change.sha,
        },
        source_title=change.title,
        source_url=change.url,
        target=target,
        target_repo=sdk.repo,
        target_base_sha=(
            revision.head_sha
            if revision is not None
            else client.repository_head(sdk.repo)
        ),
        tracking_issue=int(str(issue["number"])),
        tracking_issue_url=str(issue.get("html_url") or issue.get("url") or ""),
        by=actor,
        idempotency_key=(idempotency_key or f"propagate:{change.source_id}:{target}"),
        branch=f"agricola/{change.source_id.replace('#', '-')}",
        verify=sdk.verify,
        changelog=sdk.changelog,
        owners=sdk.owners,
        plan=plan,
        instruction=instruction,
        revision=revision,
    )


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
