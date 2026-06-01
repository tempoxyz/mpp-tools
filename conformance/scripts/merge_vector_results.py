#!/usr/bin/env python3
"""Merge per-adapter vector conformance result artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


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


def adapters(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def format_errors(errors: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for error in errors[:20]:
        lines.append(
            "- {adapter}::{vector}::{test_type}::{test_name}".format(
                adapter=error.get("adapter", "unknown"),
                vector=error.get("vector", "runner"),
                test_type=error.get("test_type", "build"),
                test_name=error.get("test_name", "runner"),
            )
        )
        detail = str(error.get("error") or "")
        if detail:
            lines.append(f"  {detail[:200]}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapters", required=True)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    merged: dict[str, object] = {
        "status": "pass",
        "num_checks": 0,
        "passed": 0,
        "failed": 0,
        "errors": [],
    }
    errors = merged["errors"]
    assert isinstance(errors, list)

    for adapter in adapters(args.adapters):
        result_path = args.result_dir / f"results-{adapter}.json"
        stderr_path = args.result_dir / f"stderr-{adapter}.log"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            stderr = stderr_path.read_text(encoding="utf-8").strip() if stderr_path.exists() else ""
            merged["failed"] = int(merged["failed"]) + 1
            merged["num_checks"] = int(merged["num_checks"]) + 1
            errors.append(
                {
                    "adapter": adapter,
                    "vector": "runner",
                    "test_type": "build",
                    "test_name": "runner",
                    "error": stderr or f"Could not read {result_path}: {exc}",
                }
            )
            continue

        merged["passed"] = int(merged["passed"]) + int(result.get("passed", 0))
        merged["failed"] = int(merged["failed"]) + int(result.get("failed", 0))
        merged["num_checks"] = int(merged["num_checks"]) + int(result.get("num_checks", 0))
        errors.extend(result.get("errors", []))

    if int(merged["failed"]):
        merged["status"] = "fail"

    args.output.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    error_text = format_errors(errors)
    output(
        {
            "status": str(merged["status"]),
            "passed": str(merged["passed"]),
            "failed": str(merged["failed"]),
            "total": str(merged["num_checks"]),
            "errors": error_text,
        }
    )
    print(f"Vector status: {merged['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
