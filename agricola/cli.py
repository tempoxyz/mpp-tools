from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from .audit import (
    AuditError,
    audit_matrix,
    build_audit_report,
    deliver_audit_report,
    load_snapshots,
    read_json_object,
    render_audit_report,
    snapshot_from_conformance,
)
from .commands import parse_commands, require_maintainer
from .executor import (
    VerificationError,
    pull_request_body,
    pull_request_title,
    verify,
)
from .github import GitHubClient
from .ledger import AuditStore, CursorStore, DecisionLedger, LedgerError
from .manifest import ManifestError, load_manifest, print_schemas
from .models import (
    AuditSemanticResult,
    PendingIssueUpdate,
    PendingReply,
    PendingAuditReport,
    PropagationOutcome,
    PropagationRequest,
)
from .service import handle_comment, poll, record_propagations


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agricola")
    result.add_argument("--manifest", default="sdks.yaml", help="SDK manifest path")
    result.add_argument("--ledger", default="ledger", help="decision ledger directory")
    subcommands = result.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate", help="validate the manifest and ledger")
    subcommands.add_parser("schema", help="print generated JSON Schemas")
    subcommands.add_parser(
        "audit-semantic-schema",
        help="print the structured semantic audit result schema",
    )

    poller = subcommands.add_parser(
        "poll", help="process merged canonical pull requests"
    )
    poller.add_argument(
        "--control-repo",
        default=os.environ.get("GITHUB_REPOSITORY", "tempoxyz/mpp-tools"),
    )

    handler = subcommands.add_parser(
        "handle-comment", help="handle an issue_comment webhook payload"
    )
    handler.add_argument(
        "event", nargs="?", default=os.environ.get("GITHUB_EVENT_PATH")
    )
    handler.add_argument(
        "--control-repo",
        default=os.environ.get("GITHUB_REPOSITORY", "tempoxyz/mpp-tools"),
    )
    handler.add_argument(
        "--reply-file",
        help="defer the GitHub reply by writing it to this file",
    )
    handler.add_argument(
        "--issue-update-file",
        help="defer the tracking issue update by writing it to this file",
    )

    delivery = subcommands.add_parser(
        "deliver-reply", help="deliver a previously deferred GitHub reply"
    )
    delivery.add_argument("reply_file")
    delivery.add_argument(
        "--control-repo",
        default=os.environ.get("GITHUB_REPOSITORY", "tempoxyz/mpp-tools"),
    )

    issue_delivery = subcommands.add_parser(
        "deliver-issue-update",
        help="deliver a previously deferred tracking issue update",
    )
    issue_delivery.add_argument("update_file")
    issue_delivery.add_argument(
        "--control-repo",
        default=os.environ.get("GITHUB_REPOSITORY", "tempoxyz/mpp-tools"),
    )

    recorder = subcommands.add_parser(
        "record-propagations", help="record downstream propagation outcomes"
    )
    recorder.add_argument("results", help="directory containing propagation results")
    recorder.add_argument(
        "--reply-directory",
        required=True,
        help="directory for replies delivered after ledger persistence",
    )
    recorder.add_argument(
        "--issue-update-directory",
        required=True,
        help="directory for issue updates delivered after ledger persistence",
    )

    matrix = subcommands.add_parser(
        "audit-matrix", help="render the manifest audit matrix"
    )
    matrix.add_argument(
        "--adapters",
        default="conformance/adapters",
        help="conformance adapter directory",
    )

    snapshot = subcommands.add_parser(
        "audit-snapshot", help="normalize one head conformance result"
    )
    snapshot.add_argument("--target", required=True)
    snapshot.add_argument("--repo", required=True)
    snapshot.add_argument("--sha", required=True)
    snapshot.add_argument("--canonical-sha")
    snapshot.add_argument("--adapter-manifest", required=True)
    snapshot.add_argument("--results", required=True)
    snapshot.add_argument("--semantic-results")
    snapshot.add_argument("--semantic-error")

    builder = subcommands.add_parser(
        "build-audit", help="cluster snapshots and render the audit roll-up"
    )
    builder.add_argument("snapshots", help="directory containing audit snapshots")
    builder.add_argument("--report-file", required=True)

    audit_delivery = subcommands.add_parser(
        "deliver-audit", help="reconcile the audit index and finding issues"
    )
    audit_delivery.add_argument("report_file")
    audit_delivery.add_argument(
        "--control-repo",
        default=os.environ.get("GITHUB_REPOSITORY", "tempoxyz/mpp-tools"),
    )

    verifier = subcommands.add_parser(
        "verify-propagation", help="run manifest verification for generated changes"
    )
    verifier.add_argument("request", help="propagation request JSON")
    verifier.add_argument("--root", default=".", help="target repository directory")

    renderer = subcommands.add_parser(
        "render-propagation", help="render downstream pull-request metadata"
    )
    renderer.add_argument("request", help="propagation request JSON")
    renderer.add_argument("--title-file", required=True)
    renderer.add_argument("--body-file", required=True)

    command_parser = subcommands.add_parser(
        "parse-command", help="parse commands from stdin"
    )
    command_parser.add_argument("--author", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "schema":
        print(print_schemas(), end="")
        return 0
    if args.command == "audit-semantic-schema":
        print(json.dumps(AuditSemanticResult.model_json_schema(), indent=2))
        return 0
    if args.command == "deliver-reply":
        try:
            reply = PendingReply.model_validate_json(Path(args.reply_file).read_text())
        except (OSError, ValidationError) as exc:
            print(f"reply error: {exc}", file=sys.stderr)
            return 2
        GitHubClient(args.control_repo).comment_issue(reply.issue_number, reply.body)
        print(json.dumps({"delivered": True, "issue_number": reply.issue_number}))
        return 0
    if args.command == "deliver-issue-update":
        try:
            update = PendingIssueUpdate.model_validate_json(
                Path(args.update_file).read_text()
            )
        except (OSError, ValidationError) as exc:
            print(f"issue update error: {exc}", file=sys.stderr)
            return 2
        GitHubClient(args.control_repo).update_issue(
            update.issue_number,
            body=update.body,
        )
        print(json.dumps({"updated": True, "issue_number": update.issue_number}))
        return 0
    if args.command == "deliver-audit":
        try:
            pending = PendingAuditReport.model_validate_json(
                Path(args.report_file).read_text()
            )
            issue = deliver_audit_report(
                GitHubClient(args.control_repo),
                pending,
            )
        except (AuditError, OSError, ValidationError) as exc:
            print(f"audit report error: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "delivered": True,
                    "issue_number": issue.get("number"),
                    "url": issue.get("html_url"),
                }
            )
        )
        return 0
    if args.command == "audit-snapshot":
        semantic_result = None
        semantic_error = args.semantic_error
        if args.semantic_results:
            try:
                semantic_result = read_json_object(args.semantic_results)
            except AuditError as exc:
                semantic_error = f"{args.target}: semantic review unreadable: {exc}"
        try:
            snapshot = snapshot_from_conformance(
                target=args.target,
                repo=args.repo,
                sha=args.sha,
                adapter_manifest=read_json_object(args.adapter_manifest),
                results=read_json_object(args.results),
                canonical_sha=args.canonical_sha,
                semantic_result=semantic_result,
                semantic_error=semantic_error,
            )
        except (AuditError, ValidationError) as exc:
            print(f"audit snapshot error: {exc}", file=sys.stderr)
            return 2
        print(snapshot.model_dump_json(indent=2))
        return 0
    if args.command == "record-propagations":
        result_paths = tuple(sorted(Path(args.results).glob("*.json")))
        try:
            outcome_adapter = TypeAdapter(PropagationOutcome)
            results = tuple(
                outcome_adapter.validate_json(path.read_text()) for path in result_paths
            )
            changed, replies, updates = record_propagations(
                DecisionLedger(args.ledger), results
            )
        except (OSError, ValidationError, LedgerError) as exc:
            print(f"propagation result error: {exc}", file=sys.stderr)
            return 2
        reply_directory = Path(args.reply_directory)
        reply_directory.mkdir(parents=True, exist_ok=True)
        for reply in replies:
            (reply_directory / f"issue-{reply.issue_number}.json").write_text(
                reply.model_dump_json(indent=2) + "\n"
            )
        issue_update_directory = Path(args.issue_update_directory)
        issue_update_directory.mkdir(parents=True, exist_ok=True)
        for update in updates:
            (issue_update_directory / f"issue-{update.issue_number}.json").write_text(
                update.model_dump_json(indent=2) + "\n"
            )
        print(
            json.dumps(
                {
                    "results": len(results),
                    "changed_ledger": changed,
                    "replies": len(replies),
                    "issue_updates": len(updates),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "verify-propagation":
        try:
            request = PropagationRequest.model_validate_json(
                Path(args.request).read_text()
            )
            verify(request, args.root)
        except (OSError, ValidationError, VerificationError) as exc:
            print(f"verification error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"verified": request.target, "commands": len(request.verify)}))
        return 0
    if args.command == "render-propagation":
        try:
            request = PropagationRequest.model_validate_json(
                Path(args.request).read_text()
            )
            Path(args.title_file).write_text(pull_request_title(request) + "\n")
            Path(args.body_file).write_text(pull_request_body(request))
        except (OSError, ValidationError) as exc:
            print(f"render error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"rendered": request.target}))
        return 0
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    ledger = DecisionLedger(args.ledger)

    if args.command == "audit-matrix":
        try:
            matrix = audit_matrix(manifest, args.adapters)
        except AuditError as exc:
            print(f"audit matrix error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(matrix, indent=2))
        return 0
    if args.command == "build-audit":
        try:
            report = build_audit_report(
                manifest,
                load_snapshots(args.snapshots),
                AuditStore(args.ledger),
            )
            pending = render_audit_report(report, manifest)
            Path(args.report_file).write_text(pending.model_dump_json(indent=2) + "\n")
        except (AuditError, LedgerError, OSError, ValidationError) as exc:
            print(f"audit report error: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "findings": len(report.findings),
                    "errors": len(report.errors),
                    "healthy": pending.healthy,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "validate":
        try:
            entries = ledger.validate_all()
        except LedgerError as exc:
            print(f"ledger error: {exc}", file=sys.stderr)
            return 2
        print(
            f"valid: {len(manifest.sdks)} SDKs, {len(manifest.maintainers)} maintainer(s), {len(entries)} ledger entries"
        )
        return 0
    if args.command == "parse-command":
        body = sys.stdin.read()
        require_maintainer(args.author, manifest)
        commands = parse_commands(body, manifest)
        print(
            json.dumps(
                [command.__dict__ for command in commands], default=str, indent=2
            )
        )
        return 0

    repo_tokens = {}
    if canonical_token := os.environ.get("AGRICOLA_CANONICAL_TOKEN"):
        repo_tokens[manifest.canonical.repo] = canonical_token
    client = GitHubClient(args.control_repo, repo_tokens=repo_tokens)
    if args.command == "poll":
        result = poll(client, manifest, ledger, CursorStore(args.ledger))
        print(
            json.dumps(
                {
                    **{
                        name: value
                        for name, value in result.__dict__.items()
                        if name != "propagations"
                    },
                    "propagations": [
                        request.model_dump(mode="json")
                        for request in result.propagations
                    ],
                },
                indent=2,
            )
        )
        return 0
    if args.command == "handle-comment":
        if not args.event:
            print(
                "event path is required (argument or GITHUB_EVENT_PATH)",
                file=sys.stderr,
            )
            return 2
        event = json.loads(Path(args.event).read_text())
        result = handle_comment(client, manifest, ledger, event)
        if result.reply is not None:
            if args.reply_file:
                Path(args.reply_file).write_text(
                    result.reply.model_dump_json(indent=2) + "\n"
                )
            else:
                client.comment_issue(result.reply.issue_number, result.reply.body)
        if result.issue_update is not None:
            if args.issue_update_file:
                Path(args.issue_update_file).write_text(
                    result.issue_update.model_dump_json(indent=2) + "\n"
                )
            else:
                client.update_issue(
                    result.issue_update.issue_number,
                    body=result.issue_update.body,
                )
        print(
            json.dumps(
                {
                    "commands": result.commands,
                    "changed_ledger": result.changed_ledger,
                    "ignored": result.ignored,
                    "reply_deferred": result.reply is not None
                    and bool(args.reply_file),
                    "issue_update_deferred": result.issue_update is not None
                    and bool(args.issue_update_file),
                    "propagations": [
                        request.model_dump(mode="json")
                        for request in result.propagations
                    ],
                },
                indent=2,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
