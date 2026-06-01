#!/usr/bin/env python3
"""Merge per-adapter flow conformance result artifacts."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapters", required=True)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status = "pass"
    errors: list[str] = []
    sdk_summaries: list[str] = []
    merged = []

    for adapter in adapters(args.adapters):
        result_path = args.result_dir / f"flow-{adapter}.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            status = "fail"
            errors.append(f"- {adapter}::flow\n  Could not read {result_path}: {exc}")
            continue

        merged.append(result)
        if result.get("status") != "pass":
            status = "fail"
            detail = str(result.get("errors") or "")
            errors.append(f"- {adapter}::flow")
            if detail:
                errors.append(f"  {detail[:200]}")
        if result.get("sdk_summary"):
            sdk_summaries.append(str(result["sdk_summary"]))

    args.output.write_text(json.dumps({"status": status, "results": merged}, indent=2), encoding="utf-8")
    output(
        {
            "status": status,
            "errors": "\n".join(errors[:40]),
            "sdk_summary": " ".join(sdk_summaries),
        }
    )
    print(f"Flow status: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
