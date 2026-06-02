#!/usr/bin/env python3
"""Enforce conformance coverage references for SDK protocol changes."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any


CONFORMANCE_PR_RE = re.compile(
    r"^\s*Conformance-PR:\s*(?:https://github\.com/tempoxyz/mpp-tools/pull/|tempoxyz/mpp-tools#|#)?(?P<number>\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split_patterns(value: str) -> list[str]:
    patterns: list[str] = []
    for line in value.replace(",", "\n").splitlines():
        pattern = line.strip()
        if pattern and not pattern.startswith("#"):
            patterns.append(pattern)
    return patterns


def path_matches(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    normalized_pattern = pattern.strip("/")
    if not normalized_pattern:
        return False
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return normalized == prefix or normalized.startswith(prefix + "/")
    if normalized_pattern.endswith("/"):
        return normalized.startswith(normalized_pattern)
    return fnmatchcase(normalized, normalized_pattern)


def matched_protocol_files(files: list[str], patterns: list[str]) -> list[str]:
    return [path for path in files if any(path_matches(path, pattern) for pattern in patterns)]


def pull_request(event: dict[str, Any]) -> dict[str, Any] | None:
    pr = event.get("pull_request")
    return pr if isinstance(pr, dict) else None


def pull_request_body(pr: dict[str, Any] | None) -> str:
    if not pr:
        return ""
    body = pr.get("body")
    return body if isinstance(body, str) else ""


def pull_request_labels(pr: dict[str, Any] | None) -> set[str]:
    if not pr:
        return set()
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return set()
    names: set[str] = set()
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.add(label["name"])
    return names


def output(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value:
                handle.write(f"{key}<<EOF\n{value}\nEOF\n")
            else:
                handle.write(f"{key}={value}\n")


def fail(message: str, outputs: dict[str, str]) -> int:
    output(outputs)
    print(message, file=sys.stderr)
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-path", required=True, type=Path)
    parser.add_argument("--changed-files", required=True, type=Path)
    parser.add_argument("--protocol-paths", required=True)
    parser.add_argument("--default-conformance-ref", default="main")
    parser.add_argument("--skip-label", default="conformance-not-needed")
    parser.add_argument("--require-reference", default="false")
    return parser.parse_args()


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"invalid boolean value {value!r}")


def main() -> int:
    args = parse_args()
    event = load_json(args.event_path)
    pr = pull_request(event)
    default_ref = args.default_conformance_ref
    patterns = split_patterns(args.protocol_paths)
    try:
        require_reference = parse_bool(args.require_reference)
    except ValueError as exc:
        return fail(str(exc), {"reason": "Invalid require-reference value."})

    base_outputs = {
        "conformance_ref": default_ref,
        "conformance_pr": "",
        "protocol_changed": "false",
        "matched_files": "",
        "reason": "No protocol-sensitive files changed.",
    }

    if pr is None:
        base_outputs["reason"] = "Not a pull request event; policy check is advisory only."
        output(base_outputs)
        print(base_outputs["reason"])
        return 0

    if not patterns:
        return fail(
            "No protocol-paths were configured for the SDK conformance policy.",
            {**base_outputs, "reason": "Missing protocol path configuration."},
        )

    changed_files = read_lines(args.changed_files)
    matched_files = matched_protocol_files(changed_files, patterns)
    if not matched_files:
        output(base_outputs)
        print(base_outputs["reason"])
        return 0

    body = pull_request_body(pr)
    labels = pull_request_labels(pr)
    matched_text = "\n".join(matched_files)
    policy_outputs = {
        **base_outputs,
        "protocol_changed": "true",
        "matched_files": matched_text,
    }

    if args.skip_label in labels:
        reason = f"Maintainer label {args.skip_label!r} allows this protocol-sensitive change."
        output({**policy_outputs, "reason": reason})
        print(reason)
        return 0

    conformance_pr = CONFORMANCE_PR_RE.search(body)
    if conformance_pr:
        number = conformance_pr.group("number")
        ref = f"refs/pull/{number}/head"
        reason = f"Using mpp-tools PR #{number} as conformance ref."
        output({**policy_outputs, "conformance_ref": ref, "conformance_pr": number, "reason": reason})
        print(reason)
        return 0

    if not require_reference:
        reason = f"No Conformance-PR referenced; using default conformance ref {default_ref!r} so existing coverage must pass."
        output({**policy_outputs, "reason": reason})
        print(reason)
        return 0

    message = (
        "Protocol-sensitive files changed, but the PR does not reference conformance coverage.\n\n"
        "Add this to the PR body:\n"
        "  Conformance-PR: tempoxyz/mpp-tools#123\n"
        "\n"
        f"Or ask a maintainer to apply the {args.skip_label!r} label.\n\n"
        "Matched files:\n"
        + "\n".join(f"  - {path}" for path in matched_files)
    )
    return fail(message, {**policy_outputs, "reason": "Missing conformance coverage reference."})


if __name__ == "__main__":
    raise SystemExit(main())
