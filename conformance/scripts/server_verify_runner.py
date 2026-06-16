#!/usr/bin/env python3
"""Server verification conformance runner."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import AdapterConfig, AdapterClient, build_adapter, discover_adapters


SCRIPT_DIR = Path(__file__).parent
CONFORMANCE_DIR = SCRIPT_DIR.parent
CASE_FILE = CONFORMANCE_DIR / "server-verification" / "cases.json"


@dataclass
class RunResult:
    adapter: str
    name: str
    passed: bool
    error: str | None = None


def load_cases() -> list[dict[str, Any]]:
    parsed = json.loads(CASE_FILE.read_text())
    cases = parsed.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Invalid server verification cases payload")
    return cases


def compute_diff(expected: Any, actual: Any) -> str:
    if expected == actual:
        return ""
    return "\n".join([
        "result mismatch:",
        f"  expected: {json.dumps(expected, sort_keys=True, indent=2)}",
        f"  actual:   {json.dumps(actual, sort_keys=True, indent=2)}",
    ])


def selected_adapters(name: str, adapters: dict[str, AdapterConfig]) -> list[str]:
    if name != "all":
        return [name]
    selected = [
        adapter_name
        for adapter_name, adapter in sorted(adapters.items())
        if "server.verify" in adapter.capabilities
    ]
    if not selected:
        raise RuntimeError("No adapters declare server.verify")
    return selected


def run_adapter(adapter: AdapterConfig, cases: list[dict[str, Any]]) -> list[RunResult]:
    build_error = build_adapter(adapter)
    if build_error:
        raise RuntimeError(build_error)

    client = AdapterClient(adapter)
    results: list[RunResult] = []
    for case in cases:
        name = str(case.get("name"))
        response = client.call(
            "server.verify",
            case.get("input"),
            context={"caseName": name},
        )
        if not response.get("ok"):
            error = response.get("error") or {}
            results.append(RunResult(
                adapter=adapter.name,
                name=name,
                passed=False,
                error=str(error.get("message") or error),
            ))
            continue

        expected = case.get("expected")
        actual = response.get("value")
        diff = compute_diff(expected, actual)
        results.append(RunResult(adapter=adapter.name, name=name, passed=diff == "", error=diff or None))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run server verification conformance tests")
    parser.add_argument("--adapter", default="all")
    args = parser.parse_args()

    adapters = discover_adapters()
    cases = load_cases()
    results: list[RunResult] = []

    try:
        adapter_names = selected_adapters(args.adapter, adapters)
    except Exception as exc:
        results.append(RunResult(adapter=args.adapter, name="adapter-selection", passed=False, error=str(exc)))
        adapter_names = []

    for adapter_name in adapter_names:
        adapter = adapters.get(adapter_name)
        if adapter is None:
            results.append(RunResult(adapter=adapter_name, name="adapter-run", passed=False, error="Unknown adapter"))
            continue
        print(f"Running {adapter_name} server verification...", end="", flush=True)
        try:
            adapter_results = run_adapter(adapter, cases)
            results.extend(adapter_results)
            failed = sum(1 for result in adapter_results if not result.passed)
            print(" ok" if failed == 0 else " failed")
        except Exception as exc:
            print(" failed")
            results.append(RunResult(adapter=adapter_name, name="adapter-run", passed=False, error=str(exc)))

    passed = sum(1 for result in results if result.passed)
    failed = sum(1 for result in results if not result.passed)
    total = len(results)

    print("")
    print("-" * 60)
    if failed:
        print(f"FAILED: {passed} passed, {failed} failed, {total} total")
        print("")
        print("Failures:")
        print("")
        for index, result in enumerate((result for result in results if not result.passed), start=1):
            print(f"  {index}) {result.adapter}::{result.name}")
            if result.error:
                for line in result.error.split("\n"):
                    print(f"       {line}")
            print("")
        return 1

    print(f"PASSED: {passed} passed, {total} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
