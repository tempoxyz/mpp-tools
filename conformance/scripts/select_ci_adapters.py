#!/usr/bin/env python3
"""Select conformance adapters affected by a pull request."""

from __future__ import annotations

import argparse
import json
import os
from fnmatch import fnmatchcase
from pathlib import Path


ADAPTER_TARGETS = {
    "typescript": "install-ts",
    "rust": "install-rust",
    "python": "install-python",
    "go": "install-go",
    "ruby": "install-ruby",
    "java": "install-java",
}
ADAPTER_ORDER = list(ADAPTER_TARGETS)

ADAPTER_PATTERNS = {
    "typescript": [
        "conformance/adapters/typescript/**",
        "conformance/golden/**",
    ],
    "rust": ["conformance/adapters/rust/**"],
    "python": ["conformance/adapters/python/**"],
    "go": ["conformance/adapters/go/**"],
    "ruby": ["conformance/adapters/ruby/**"],
    "java": ["conformance/adapters/java/**"],
}

FULL_PATTERNS = [
    ".github/actions/**",
    ".github/workflows/**",
    "conformance/Makefile",
    "conformance/examples/**",
    "conformance/flows/**",
    "conformance/operations.json",
    "conformance/package-lock.json",
    "conformance/package.json",
    "conformance/requirements.txt",
    "conformance/schemas/**",
    "conformance/scripts/**",
    "conformance/server-verification/**",
    "conformance/tsconfig.json",
    "conformance/vectors/**",
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


def read_changed_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def matrix(adapters: list[str]) -> list[dict[str, str]]:
    return [{"adapter": adapter, "install-target": ADAPTER_TARGETS[adapter]} for adapter in adapters]


def output(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    for key, value in values.items():
        print(f"{key}={value}")


def select_adapters(event_name: str, changed_files: list[str]) -> tuple[list[str], str]:
    if event_name != "pull_request":
        return ADAPTER_ORDER, "non-PR event runs the full suite"
    if not changed_files:
        return ADAPTER_ORDER, "no changed file list; running the full suite"
    if any(any(path_matches(path, pattern) for pattern in FULL_PATTERNS) for path in changed_files):
        return ADAPTER_ORDER, "shared conformance files changed"

    selected = [
        adapter
        for adapter in ADAPTER_ORDER
        if any(
            path_matches(path, pattern)
            for path in changed_files
            for pattern in ADAPTER_PATTERNS[adapter]
        )
    ]
    if selected:
        return selected, "adapter-specific conformance files changed"
    return [], "no conformance-affecting files changed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--changed-files", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    adapters, reason = select_adapters(args.event_name, read_changed_files(args.changed_files))
    output(
        {
            "adapters_csv": ",".join(adapters),
            "adapters_json": json.dumps(matrix(adapters), separators=(",", ":")),
            "run_conformance": "true" if adapters else "false",
            "reason": reason,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
