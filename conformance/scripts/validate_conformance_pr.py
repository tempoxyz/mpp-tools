#!/usr/bin/env python3
"""Validate that a referenced conformance PR touches coverage files."""

from __future__ import annotations

import argparse
import json
from fnmatch import fnmatchcase
from pathlib import Path


def split_patterns(value: str) -> list[str]:
    return [
        line.strip()
        for line in value.replace(",", "\n").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def path_matches(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    normalized_pattern = pattern.strip("/")
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return normalized == prefix or normalized.startswith(prefix + "/")
    if normalized_pattern.endswith("/"):
        return normalized.startswith(normalized_pattern)
    return fnmatchcase(normalized, normalized_pattern)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", required=True, type=Path)
    parser.add_argument("--files", required=True, type=Path)
    parser.add_argument("--conformance-paths", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pr = json.loads(args.pr_json.read_text(encoding="utf-8"))
    state = pr.get("state")
    merged_at = pr.get("merged_at")
    if state != "open" and not merged_at:
        print("Referenced Conformance-PR must be open or merged.")
        print(f"State: {state}, merged_at: {merged_at}")
        return 1

    files = [line.strip() for line in args.files.read_text(encoding="utf-8").splitlines() if line.strip()]
    patterns = split_patterns(args.conformance_paths)
    matched = [path for path in files if any(path_matches(path, pattern) for pattern in patterns)]
    if matched:
        print("Referenced Conformance-PR touches coverage files:")
        for path in matched:
            print(f"  - {path}")
        return 0

    print("Referenced Conformance-PR does not touch any configured conformance coverage paths.")
    print("Expected one of:")
    for pattern in patterns:
        print(f"  - {pattern}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
