from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .commands import parse_commands, require_maintainer
from .github import GitHubClient
from .ledger import CursorStore, DecisionLedger, LedgerError
from .manifest import ManifestError, load_manifest, print_schemas
from .models import PendingReply
from .service import handle_comment, poll


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

    client = GitHubClient(args.control_repo)
    if args.command == "poll":
        result = poll(client, manifest, ledger, CursorStore(args.ledger))
        print(json.dumps(result.__dict__, indent=2))
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
                },
                indent=2,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
