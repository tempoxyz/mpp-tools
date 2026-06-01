#!/usr/bin/env python3
"""Write a flow conformance result artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--status-code", required=True, type=int)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--sdk-summary", default="")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lines = args.log.read_text(encoding="utf-8", errors="replace").splitlines() if args.log.exists() else []
    result = {
        "adapter": args.adapter,
        "status": "pass" if args.status_code == 0 else "fail",
        "errors": "\n".join(lines[:60]) if args.status_code else "",
        "sdk_summary": args.sdk_summary,
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Flow status for {args.adapter}: {result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
