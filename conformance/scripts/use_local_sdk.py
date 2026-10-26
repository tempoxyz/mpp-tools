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
import os
import re
import subprocess
import tomllib
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
    # Keep the adapter's mpp features: tempo.receipt.verify needs the
    # server-side Tempo verifier (server and tempo), not only client.
    features_match = re.search(
        r"^mpp\s*=.*features\s*=\s*(\[[^\]]*\])", manifest.read_text(encoding="utf-8"), re.MULTILINE
    )
    features = features_match.group(1) if features_match else '["client"]'
    replace_one(
        manifest,
        r"^mpp\s*=\s*.+$",
        f"mpp = {{ path = {json_string(sdk_path)}, features = {features} }}",
        "Rust mpp dependency",
    )
    reqwest_version = rust_sdk_reqwest_version(sdk_path)
    if reqwest_version is not None:
        # The adapter hands its own reqwest::RequestBuilder to the SDK's Fetch
        # trait, so both must resolve to the same reqwest crate.
        replace_one(
            manifest,
            r"^reqwest\s*=\s*.+$",
            f"reqwest = {{ version = {json.dumps(reqwest_version)}, default-features = false }}",
            "Rust reqwest dependency",
        )


def rust_sdk_reqwest_version(sdk_path: Path) -> str | None:
    """Return the reqwest version requirement the SDK checkout declares."""
    manifest = sdk_path / "Cargo.toml"
    if not manifest.exists():
        return None
    cargo = tomllib.loads(read_text(manifest))
    dependency = cargo.get("dependencies", {}).get("reqwest")
    if isinstance(dependency, dict) and dependency.get("workspace") is True:
        dependency = cargo.get("workspace", {}).get("dependencies", {}).get("reqwest")
    if isinstance(dependency, str):
        return dependency
    if isinstance(dependency, dict) and isinstance(dependency.get("version"), str):
        return dependency["version"]
    return None


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
        r'^(\s*)"pympp(\[[^\]]*\])?[^"]*",\s*$',
        rf'\1"pympp\2 @ {sdk_path.as_uri()}",',
        "Python pympp dependency",
    )


def go_sdk_version(sdk_path: Path) -> str:
    go_mod = read_text(sdk_path / "go.mod")
    match = re.search(r"^// changelogs:version\s+(v?\S+)\s*$", go_mod, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not find changelogs:version in {sdk_path / 'go.mod'}")
    return match.group(1) if match.group(1).startswith("v") else f"v{match.group(1)}"


def configure_go(conformance_dir: Path, sdk_path: Path) -> None:
    adapter_dir = conformance_dir / "adapters" / "go"
    go_env = os.environ.copy()
    go_env.setdefault("GOTOOLCHAIN", "auto")
    sdk_version = go_sdk_version(sdk_path)
    result = subprocess.run(
        [
            "go",
            "mod",
            "edit",
            "-require",
            f"github.com/tempoxyz/mpp-go@{sdk_version}",
            "-replace",
            f"github.com/tempoxyz/mpp-go={sdk_path}",
        ],
        cwd=adapter_dir,
        env=go_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"go mod edit failed: {detail}")

    result = subprocess.run(
        ["go", "mod", "tidy"],
        cwd=adapter_dir,
        env=go_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"go mod tidy failed: {detail}")


def configure_typescript(conformance_dir: Path, sdk_path: Path) -> None:
    package_json = conformance_dir / "package.json"
    data = json.loads(read_text(package_json))
    data.setdefault("dependencies", {})["mppx"] = f"file:{sdk_path}"
    write_text(package_json, json.dumps(data, indent=2) + "\n")


def configure_java(conformance_dir: Path, sdk_path: Path) -> None:
    """Point the Java adapter at a pre-built local SDK jar.

    Unlike other adapters that can reference a source checkout directly via
    their package manager (e.g. `gem ... path:`, `go mod edit -replace`),
    Gradle composite builds fail here due to version incompatibility between
    the adapter's Gradle (9.x) and the SDK's plugins. Instead we:
      1. Require the caller to build the SDK jar first (./gradlew jar)
      2. Replace the Maven Central dependency with a files() reference to that jar
      3. Remove the Gradle lockfile which would reject the missing coordinate
    """
    adapter_dir = conformance_dir / "adapters" / "java"
    build_gradle = adapter_dir / "build.gradle"
    libs_dir = sdk_path / "build" / "libs"

    if not libs_dir.exists():
        raise RuntimeError(
            f"SDK build/libs directory does not exist: {libs_dir}\n"
            f"Build the SDK jar first: cd {sdk_path} && ./gradlew jar"
        )

    jars = [
        f
        for f in libs_dir.iterdir()
        if f.suffix == ".jar"
        and "-sources" not in f.name
        and "-javadoc" not in f.name
    ]
    if not jars:
        raise RuntimeError(
            f"No SDK jar found in {libs_dir}\n"
            f"Build the SDK jar first: cd {sdk_path} && ./gradlew jar"
        )

    sdk_jar = jars[0]
    replace_one(
        build_gradle,
        r"^\s*implementation\s+['\"]com\.(stripe|github\.stripe):mpp-java:.*['\"]",
        f"    implementation files('{sdk_jar}')",
        "Java mpp-java dependency",
    )

    # The lockfile pins the Maven Central coordinate; remove it so Gradle
    # doesn't reject the build when that coordinate is replaced with files().
    lockfile = adapter_dir / "gradle.lockfile"
    if lockfile.exists():
        lockfile.unlink()


CONFIGURERS = {
    "go": configure_go,
    "java": configure_java,
    "python": configure_python,
    "ruby": configure_ruby,
    "rust": configure_rust,
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
