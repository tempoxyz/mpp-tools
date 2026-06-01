#!/usr/bin/env python3
"""Print the pinned SDK package version for an adapter."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd or ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def typescript_version() -> str:
    version = run(["node", "-p", "require('./node_modules/mppx/package.json').version"])
    return f"mppx@{version}"


def rust_version() -> str:
    output = run(
        [
            "cargo",
            "metadata",
            "--locked",
            "--manifest-path",
            "adapters/rust/Cargo.toml",
            "--format-version",
            "1",
        ]
    )
    data = json.loads(output)
    version = next(package["version"] for package in data["packages"] if package["name"] == "mpp")
    return f"mpp@{version}"


def python_version() -> str:
    version = run(
        [
            "uv",
            "run",
            "--locked",
            "--python",
            "3.12",
            "python",
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('pympp'))",
        ],
        cwd=ROOT / "adapters/python",
    )
    return f"pympp@{version}"


def go_version() -> str:
    version = run(
        ["go", "list", "-m", "-f", "{{.Version}}", "github.com/tempoxyz/mpp-go"],
        cwd=ROOT / "adapters/go",
    )
    return f"github.com/tempoxyz/mpp-go@{version}"


def ruby_version() -> str:
    env = {**os.environ, "BUNDLE_PATH": "vendor/bundle"}
    version = run(
        ["bundle", "exec", "ruby", "-e", "puts Gem.loaded_specs.fetch('mpp-rb').version"],
        cwd=ROOT / "adapters/ruby",
        env=env,
    )
    return f"mpp-rb@{version}"


def java_version() -> str:
    text = (ROOT / "adapters/java/build.gradle").read_text(encoding="utf-8")
    match = re.search(r"com\.github\.stripe:mpp-java:([^'\"]+)", text)
    if not match:
        raise RuntimeError("Could not find mpp-java dependency in adapters/java/build.gradle")
    return f"com.github.stripe:mpp-java@{match.group(1)}"


VERSIONS = {
    "typescript": typescript_version,
    "rust": rust_version,
    "python": python_version,
    "go": go_version,
    "ruby": ruby_version,
    "java": java_version,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, choices=sorted(VERSIONS))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(VERSIONS[args.adapter]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
