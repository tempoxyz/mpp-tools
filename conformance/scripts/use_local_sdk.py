#!/usr/bin/env python3
"""Point a conformance adapter at a local SDK checkout.

The default conformance adapters test pinned published SDK packages. SDK repo
CI needs the same adapters to test the pull request checkout instead. This
script applies the package-manager override for the selected adapter inside an
ephemeral checkout.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
CONFORMANCE_DIR = SCRIPT_DIR.parent


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def replace_one(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = read_text(path)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Could not find {label} in {path}")
    write_text(path, updated)


def json_string(value: Path | str) -> str:
    return json.dumps(str(value))


def configure_rust(conformance_dir: Path, sdk_path: Path) -> None:
    manifest = conformance_dir / "adapters" / "rust" / "Cargo.toml"
    replace_one(
        manifest,
        r"^mpp\s*=\s*.+$",
        f"mpp = {{ path = {json_string(sdk_path)} }}",
        "Rust mpp dependency",
    )


def configure_ruby(conformance_dir: Path, sdk_path: Path) -> None:
    gemfile = conformance_dir / "adapters" / "ruby" / "Gemfile"
    replace_one(
        gemfile,
        r"^gem\s+[\"']mpp-rb[\"'].*$",
        f"gem \"mpp-rb\", path: {json_string(sdk_path)}",
        "Ruby mpp-rb dependency",
    )


def configure_python(conformance_dir: Path, sdk_path: Path) -> None:
    pyproject = conformance_dir / "adapters" / "python" / "pyproject.toml"
    replace_one(
        pyproject,
        r'^(\s*)"pympp[^"]*",\s*$',
        rf'\1"pympp @ {sdk_path.as_uri()}",',
        "Python pympp dependency",
    )


def configure_go(conformance_dir: Path, sdk_path: Path) -> None:
    adapter_dir = conformance_dir / "adapters" / "go"
    result = subprocess.run(
        [
            "go",
            "mod",
            "edit",
            "-replace",
            f"github.com/tempoxyz/mpp-go={sdk_path}",
        ],
        cwd=adapter_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"go mod edit failed: {detail}")


def configure_typescript(conformance_dir: Path, sdk_path: Path) -> None:
    package_json = conformance_dir / "package.json"
    data = json.loads(read_text(package_json))
    data.setdefault("dependencies", {})["mppx"] = f"file:{sdk_path}"
    write_text(package_json, json.dumps(data, indent=2) + "\n")


def configure_swift(conformance_dir: Path, sdk_path: Path) -> None:
    manifest = conformance_dir / "adapters" / "swift" / "Package.swift"
    replace_one(
        manifest,
        r'^(\s*)\.package\(url:\s*"https://github\.com/tempoxyz/mpp-swift\.git",\s*(?:branch|revision):\s*"[^"]+"\),\s*$',
        rf'\1.package(path: {json_string(sdk_path)}),',
        "Swift mpp-swift dependency",
    )


CONFIGURERS = {
    "go": configure_go,
    "python": configure_python,
    "ruby": configure_ruby,
    "rust": configure_rust,
    "swift": configure_swift,
    "typescript": configure_typescript,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, help="Adapter name, for example rust or ruby")
    parser.add_argument("--sdk-path", required=True, type=Path, help="Path to the SDK checkout")
    parser.add_argument(
        "--conformance-dir",
        type=Path,
        default=CONFORMANCE_DIR,
        help="Path to the conformance directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    adapter = args.adapter.strip().lower()
    conformance_dir = args.conformance_dir.resolve()
    sdk_path = args.sdk_path.resolve()

    manifest = conformance_dir / "adapters" / adapter / "adapter.json"
    if not manifest.exists():
        supported = ", ".join(sorted(CONFIGURERS))
        raise RuntimeError(
            f"No conformance adapter manifest exists for {adapter!r}. "
            f"Currently configurable adapters: {supported}."
        )

    configurer = CONFIGURERS.get(adapter)
    if configurer is None:
        supported = ", ".join(sorted(CONFIGURERS))
        raise RuntimeError(f"No local SDK override is implemented for {adapter!r}; supported: {supported}")

    if not sdk_path.exists():
        raise RuntimeError(f"SDK path does not exist: {sdk_path}")

    configurer(conformance_dir, sdk_path)
    print(f"Configured {adapter} adapter to use {sdk_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
