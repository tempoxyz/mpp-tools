from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .commands import parse_commands, require_maintainer
from .executor import (
    VerificationError,
    pull_request_body,
    pull_request_title,
    verify,
)
from .github import GitHubClient
from .ledger import CursorStore, DecisionLedger, LedgerError
from .manifest import ManifestError, load_manifest, print_schemas
from .models import PendingReply, PropagationRequest, PropagationResult
from .service import handle_comment, poll, record_propagations


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agricola")
    result.add_argument("--manifest", default="sdks.yaml", help="SDK manifest path")
    result.add_argument("--ledger", default="ledger", help="decision ledger directory")
    subcommands = result.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate", help="validate the manifest and ledger")
    subcommands.add_parser("schema", help="print generated JSON Schemas")

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

    delivery = subcommands.add_parser(
        "deliver-reply", help="deliver a previously deferred GitHub reply"
    )
    delivery.add_argument("reply_file")
    delivery.add_argument(
        "--control-repo",
        default=os.environ.get("GITHUB_REPOSITORY", "tempoxyz/mpp-tools"),
    )

    recorder = subcommands.add_parser(
        "record-propagations", help="record published downstream pull requests"
    )
    recorder.add_argument("results", help="directory containing propagation results")
    recorder.add_argument(
        "--reply-directory",
        required=True,
        help="directory for replies delivered after ledger persistence",
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
    if args.command == "deliver-reply":
        try:
            reply = PendingReply.model_validate_json(Path(args.reply_file).read_text())
        except (OSError, ValidationError) as exc:
            print(f"reply error: {exc}", file=sys.stderr)
            return 2
        GitHubClient(args.control_repo).comment_issue(reply.issue_number, reply.body)
        print(json.dumps({"delivered": True, "issue_number": reply.issue_number}))
        return 0
    if args.command == "record-propagations":
        result_paths = tuple(sorted(Path(args.results).glob("*.json")))
        try:
            results = tuple(
                PropagationResult.model_validate_json(path.read_text())
                for path in result_paths
            )
            changed, replies = record_propagations(DecisionLedger(args.ledger), results)
        except (OSError, ValidationError, LedgerError) as exc:
            print(f"propagation result error: {exc}", file=sys.stderr)
            return 2
        reply_directory = Path(args.reply_directory)
        reply_directory.mkdir(parents=True, exist_ok=True)
        for reply in replies:
            (reply_directory / f"issue-{reply.issue_number}.json").write_text(
                reply.model_dump_json(indent=2) + "\n"
            )
        print(
            json.dumps(
                {
                    "results": len(results),
                    "changed_ledger": changed,
                    "replies": len(replies),
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
        print(
            json.dumps(
                {
                    "commands": result.commands,
                    "changed_ledger": result.changed_ledger,
                    "ignored": result.ignored,
                    "reply_deferred": result.reply is not None
                    and bool(args.reply_file),
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
