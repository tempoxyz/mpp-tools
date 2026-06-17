#!/usr/bin/env python3
"""
Vector Test Runner for MPP conformance testing.

Reads test vectors from vectors/*.json, invokes each registered adapter,
compares outputs to expected results, and produces a pass/fail summary.

Usage:
    python3 scripts/vector_runner.py [--adapter NAME] [--verbose] [--vector NAME]

Options:
    --adapter NAME    Run only the specified adapter (default: all)
    --verbose         Show detailed output for each test
    --vector NAME     Run only tests from specified vector file (e.g., www-authenticate)
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from deepdiff import DeepDiff

from conformance_checks import make_check
from harness import AdapterClient, AdapterConfig, build_adapter, discover_adapters


SCRIPT_DIR = Path(__file__).parent


class DiffType(str, Enum):
    """Enum for DeepDiff dictionary keys."""
    VALUES_CHANGED = "values_changed"
    DICTIONARY_ITEM_ADDED = "dictionary_item_added"
    DICTIONARY_ITEM_REMOVED = "dictionary_item_removed"
    TYPE_CHANGES = "type_changes"
    ITERABLE_ITEM_ADDED = "iterable_item_added"
    ITERABLE_ITEM_REMOVED = "iterable_item_removed"


class TestType(str, Enum):
    """Enum for test types."""
    BUILD = "build"
    PARSE = "parse"
    FORMAT = "format"
    ROUNDTRIP = "roundtrip"
    GENERATE = "generate"
    OPERATION = "operation"


def base64url_decode(s: str) -> bytes:
    """Decode base64url (no padding) to bytes."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def compute_diff(expected: Any, actual: Any) -> str:
    """Compute a human-readable diff between expected and actual values."""
    diff = DeepDiff(expected, actual, ignore_order=True, verbose_level=2)
    if not diff:
        return ""
    lines = []
    if DiffType.VALUES_CHANGED in diff:
        for path, change in diff[DiffType.VALUES_CHANGED].items():
            lines.append(f"  {path}: {change['old_value']!r} → {change['new_value']!r}")
    if DiffType.DICTIONARY_ITEM_ADDED in diff:
        for path in diff[DiffType.DICTIONARY_ITEM_ADDED]:
            value = diff[DiffType.DICTIONARY_ITEM_ADDED][path]
            lines.append(f"  + {path}: {value!r}")
    if DiffType.DICTIONARY_ITEM_REMOVED in diff:
        for path in diff[DiffType.DICTIONARY_ITEM_REMOVED]:
            value = diff[DiffType.DICTIONARY_ITEM_REMOVED][path]
            lines.append(f"  - {path}: {value!r}")
    if DiffType.TYPE_CHANGES in diff:
        for path, change in diff[DiffType.TYPE_CHANGES].items():
            lines.append(f"  {path}: type {change['old_type'].__name__} → {change['new_type'].__name__}")
            lines.append(f"    {change['old_value']!r} → {change['new_value']!r}")
    if DiffType.ITERABLE_ITEM_ADDED in diff:
        for path in diff[DiffType.ITERABLE_ITEM_ADDED]:
            lines.append(f"  + {path}: {diff[DiffType.ITERABLE_ITEM_ADDED][path]!r}")
    if DiffType.ITERABLE_ITEM_REMOVED in diff:
        for path in diff[DiffType.ITERABLE_ITEM_REMOVED]:
            lines.append(f"  - {path}: {diff[DiffType.ITERABLE_ITEM_REMOVED][path]!r}")
    return "\n".join(lines)


def format_mismatch_error(expected: Any, actual: Any, label: str = "result") -> str:
    """Format a mismatch error with both full shapes and diff."""
    diff_str = compute_diff(expected, actual)
    error_parts = [
        f"{label} mismatch:",
        f"  expected: {json.dumps(expected, sort_keys=True, indent=2)}",
        f"  actual:   {json.dumps(actual, sort_keys=True, indent=2)}",
    ]
    if diff_str:
        error_parts.append(f"  diff:")
        for line in diff_str.split("\n"):
            error_parts.append(f"  {line}")
    return "\n".join(error_parts)


CONFORMANCE_DIR = SCRIPT_DIR.parent
VECTORS_DIR = CONFORMANCE_DIR / "vectors"


def discover_vector_files() -> dict[str, Path]:
    """Auto-discover all vector JSON files in the vectors directory."""
    vectors = {}
    for path in VECTORS_DIR.glob("*.json"):
        if path.stem == "package":
            continue
        vectors[path.stem] = path
    return vectors


@dataclass
class TestResult:
    """Result of a single test case."""
    vector_file: str
    test_type: TestType
    test_name: str
    adapter: str
    passed: bool
    description: str | None = None
    tags: list[str] | None = None
    spec_ref: str | None = None
    expected: Any = None
    actual: Any = None
    error: str | None = None

    def to_check(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "adapter": self.adapter,
            "vector": self.vector_file,
            "testType": self.test_type.value,
            "scenario": self.test_name,
        }
        if self.tags:
            details["tags"] = self.tags
        if not self.passed:
            details["expected"] = self.expected
            details["actual"] = self.actual

        return make_check(
            id_parts=[
                "vector",
                self.vector_file,
                self.test_name,
                self.test_type.value,
                self.adapter,
            ],
            name=f"{self.adapter} {self.vector_file} {self.test_name} {self.test_type.value}",
            description=self.description
            or f"{self.test_type.value} conformance for {self.test_name}",
            passed=self.passed,
            spec_ref=self.spec_ref,
            details=details,
            error=self.error,
        )


class VectorRunner:
    """Runs conformance tests against registered adapters."""

    def __init__(self, verbose: bool = False, output_format: str = "text"):
        self.verbose = verbose
        self.output_format = output_format
        self.results: list[TestResult] = []
    
    def log(self, msg: str, end: str = "\n", flush: bool = False) -> None:
        """Print message only if output format is text."""
        if self.output_format == "text":
            print(msg, end=end, flush=flush)

    def _error_result(self, message: str) -> dict[str, Any]:
        return {"success": False, "error": message, "error_type": "unknown_error"}

    def run_adapter(
        self, adapter: AdapterConfig, command: str, input_data: str, timeout: float = 30
    ) -> dict[str, Any]:
        """Run an adapter operation through the schema-backed JSON ABI."""
        try:
            return AdapterClient(adapter).run_legacy_command(command, input_data, timeout=timeout)
        except Exception as exc:
            return self._error_result(str(exc))

    def run_adapter_timed(
        self, adapter: AdapterConfig, command: str, input_data: str, timeout: float = 30
    ) -> tuple[dict[str, Any], float]:
        start = time.perf_counter()
        result = self.run_adapter(adapter, command, input_data, timeout=timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return result, elapsed_ms

    def run_operation_timed(
        self,
        adapter: AdapterConfig,
        operation: str,
        input_value: Any,
        context: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> tuple[dict[str, Any], float]:
        start = time.perf_counter()
        try:
            result = AdapterClient(adapter).call(operation, input_value, context=context, timeout=timeout)
        except Exception as exc:
            result = {"ok": False, "error": {"type": "unknown_error", "message": str(exc)}}
        elapsed_ms = (time.perf_counter() - start) * 1000
        return result, elapsed_ms

    def duration_limit_ms(self, scenario: dict[str, Any], adapter: AdapterConfig) -> int | None:
        per_adapter = scenario.get("maxDurationMsByAdapter", {})
        if isinstance(per_adapter, dict) and adapter.name in per_adapter:
            return int(per_adapter[adapter.name])
        value = scenario.get("maxDurationMs")
        return int(value) if value is not None else None

    def compare_duration(
        self, limit_ms: int | None, elapsed_ms: float
    ) -> tuple[bool, str | None]:
        if limit_ms is None or elapsed_ms <= limit_ms:
            return True, None
        return False, f"duration exceeded: expected <= {limit_ms} ms, got {elapsed_ms:.1f} ms"

    def command_timeout_seconds(self, duration_limit_ms: int | None) -> float:
        if duration_limit_ms is None:
            return 30.0
        return max(1.0, (duration_limit_ms / 1000) + 1.0)

    def scenario_wire(self, scenario: dict[str, Any]) -> str | None:
        wire = scenario.get("wire")
        if not isinstance(wire, dict):
            return wire

        prefix = wire.get("prefix", "")
        repeat = wire.get("repeat", "")
        count = int(wire.get("count", 0))
        suffix = wire.get("suffix", "")
        return f"{prefix}{repeat * count}{suffix}"

    def _compare_success_and_error_type(
        self, expected: dict[str, Any], actual: dict[str, Any]
    ) -> tuple[bool, str | None, bool]:
        expected_success = expected.get("success")
        actual_success = actual.get("success")
        if expected_success != actual_success:
            return (
                False,
                f"success mismatch: expected {expected_success}, got {actual_success}",
                False,
            )
        if not expected_success:
            expected_type = expected.get("error_type")
            actual_type = actual.get("error_type")
            if expected_type != actual_type:
                return (
                    False,
                    f"error_type mismatch: expected {expected_type}, got {actual_type}",
                    False,
                )
            return True, None, False
        return True, None, True

    def _record_result(
        self,
        *,
        vector_file: str,
        test_type: TestType,
        test_name: str,
        adapter: str,
        passed: bool,
        expected: Any,
        actual: Any,
        error: str | None,
        description: str | None = None,
        tags: list[str] | None = None,
        spec_ref: str | None = None,
    ) -> None:
        self.results.append(TestResult(
            vector_file=vector_file,
            test_type=test_type,
            test_name=test_name,
            adapter=adapter,
            passed=passed,
            description=description,
            tags=tags,
            spec_ref=spec_ref,
            expected=expected,
            actual=actual,
            error=error,
        ))

        if self.verbose:
            status = "✓" if passed else "✗"
            print(f"    {status} {test_name}")
            if not passed and error:
                print(f"      {error}")

    def compare_results(
        self, expected: dict[str, Any], actual: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Compare expected and actual results."""
        ok, error, success = self._compare_success_and_error_type(expected, actual)
        if not ok or not success:
            return ok, error

        expected_result = expected.get("result")
        actual_result = actual.get("result")
        if expected_result != actual_result:
            return False, format_mismatch_error(expected_result, actual_result)

        return True, None

    def compare_adapter_response(
        self, expected: dict[str, Any], actual: dict[str, Any]
    ) -> tuple[bool, str | None]:
        expected_ok = expected.get("ok")
        actual_ok = actual.get("ok")
        if expected_ok != actual_ok:
            return False, f"ok mismatch: expected {expected_ok}, got {actual_ok}"

        if expected_ok is False:
            expected_type = expected.get("error", {}).get("type")
            actual_type = actual.get("error", {}).get("type")
            if expected_type != actual_type:
                return False, f"error.type mismatch: expected {expected_type}, got {actual_type}"
            return True, None

        expected_value = expected.get("value")
        actual_value = actual.get("value")
        if expected_value != actual_value:
            return False, format_mismatch_error(expected_value, actual_value, "value")
        return True, None

    def normalize_credential_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Normalize a credential result for semantic comparison.
        
        Handles the case where some SDKs keep request as base64 string while
        others decode it to an object.
        """
        if not isinstance(result, dict):
            return result
        
        result = dict(result)  # shallow copy
        if "challenge" in result and isinstance(result["challenge"], dict):
            challenge = dict(result["challenge"])
            if "request" in challenge and isinstance(challenge["request"], str):
                # Decode the request field
                try:
                    decoded = base64url_decode(challenge["request"])
                    challenge["request"] = json.loads(decoded)
                except Exception:
                    pass
            result["challenge"] = challenge
        return result

    def compare_parse_results_semantic(
        self, expected: dict[str, Any], actual: dict[str, Any], command: str
    ) -> tuple[bool, str | None]:
        """Compare parse test results with semantic normalization for credentials."""
        ok, error, success = self._compare_success_and_error_type(expected, actual)
        if not ok or not success:
            return ok, error

        expected_result = expected.get("result")
        actual_result = actual.get("result")

        normalizer = {
            "parse-authorization": self.normalize_credential_result,
        }.get(command)
        if normalizer:
            expected_result = normalizer(expected_result)
            actual_result = normalizer(actual_result)

        if expected_result != actual_result:
            return False, format_mismatch_error(expected_result, actual_result)

        return True, None

    def compare_format_results_semantic(
        self,
        adapter: AdapterConfig,
        expected: dict[str, Any],
        actual: dict[str, Any],
        format_command: str,
        parse_command: str | None,
    ) -> tuple[bool, str | None]:
        """Compare formatted wire values through the adapter parser."""
        ok, error, success = self._compare_success_and_error_type(expected, actual)
        if not ok or not success:
            return ok, error

        expected_result = expected.get("result")
        actual_result = actual.get("result")

        if not parse_command or format_command == "base64url-encode":
            if expected_result != actual_result:
                return False, f"result mismatch:\n  expected: {expected_result}\n  actual:   {actual_result}"
            return True, None

        expected_parsed = self.run_adapter(adapter, parse_command, expected_result)
        actual_parsed = self.run_adapter(adapter, parse_command, actual_result)

        if not expected_parsed.get("success"):
            return False, f"semantic expected parse failed: {expected_parsed.get('error')}"
        if not actual_parsed.get("success"):
            return False, f"semantic actual parse failed: {actual_parsed.get('error')}"

        return self.compare_parse_results_semantic(expected_parsed, actual_parsed, parse_command)

    def run_vector_file(self, adapter: AdapterConfig, vector_path: Path, tag_filter: str | None = None) -> None:
        """Run all tests from a single vector file (v2 scenario format)."""
        vector_name = vector_path.stem
        if not vector_path.exists():
            print(f"  ⚠ Vector file not found: {vector_path}")
            return

        with open(vector_path) as f:
            vectors = json.load(f)

        commands = vectors.get("commands", {})
        spec_ref = vectors.get("spec_ref")
        parse_cmd = commands.get("parse")
        format_cmd = commands.get("format")
        generate_cmd = commands.get("generate")
        operation = commands.get("operation")

        if operation:
            if operation not in adapter.capabilities:
                if self.verbose:
                    print(f"  {vector_name}.json SKIPPED ({adapter.name} lacks {operation})")
                return
        elif not parse_cmd and not format_cmd and not generate_cmd:
            print(f"  ⚠ No commands defined in {vector_name}.json")
            return

        if self.verbose:
            print(f"  {vector_name}.json")

        is_base64url = parse_cmd and parse_cmd.startswith("base64url-")
        is_challenge_id = generate_cmd is not None

        for scenario in vectors.get("scenarios", []):
            scenario_adapters = scenario.get("adapters")
            if scenario_adapters and adapter.name not in scenario_adapters:
                continue

            if tag_filter and tag_filter not in scenario.get("tags", []):
                continue

            name = scenario["name"]
            description = scenario.get("description")
            tags = scenario.get("tags", [])
            tests = scenario.get("tests", {})
            duration_limit_ms = self.duration_limit_ms(scenario, adapter)
            command_timeout = self.command_timeout_seconds(duration_limit_ms)

            if operation:
                result, elapsed_ms = self.run_operation_timed(
                    adapter,
                    operation,
                    scenario["input"],
                    context={"caseName": name, "vectorName": vector_name},
                    timeout=command_timeout,
                )
                expected = scenario["expected"]
                passed, error = self.compare_adapter_response(expected, result)
                if passed:
                    passed, error = self.compare_duration(duration_limit_ms, elapsed_ms)
                self._record_result(
                    vector_file=vector_name,
                    test_type=TestType.OPERATION,
                    test_name=name,
                    adapter=adapter.name,
                    passed=passed,
                    expected=expected,
                    actual=result,
                    error=error,
                )
                continue

            if is_challenge_id:
                input_data = json.dumps(scenario["input"])
                result, elapsed_ms = self.run_adapter_timed(adapter, generate_cmd, input_data, timeout=command_timeout)
                expected = {"success": True, "result": scenario["expected"]}
                passed, error = self.compare_results(expected, result)
                if passed:
                    passed, error = self.compare_duration(duration_limit_ms, elapsed_ms)
                self._record_result(
                    vector_file=vector_name,
                    test_type=TestType.GENERATE,
                    test_name=name,
                    adapter=adapter.name,
                    passed=passed,
                    description=description,
                    tags=tags,
                    spec_ref=spec_ref,
                    expected=expected,
                    actual=result,
                    error=error,
                )
                continue

            if is_base64url:
                obj = scenario.get("decoded")
                wire = scenario.get("encoded")
            else:
                obj = scenario.get("object")
                wire = self.scenario_wire(scenario)

            # Parse test
            parse_test = tests.get("parse")
            if parse_test is not None and parse_cmd and wire is not None:
                result, elapsed_ms = self.run_adapter_timed(adapter, parse_cmd, wire, timeout=command_timeout)
                if parse_test is True:
                    expected = {"success": True, "result": obj}
                    passed, error = self.compare_parse_results_semantic(expected, result, parse_cmd)
                else:
                    expected = parse_test
                    passed, error = self.compare_results(parse_test, result)
                if passed:
                    passed, error = self.compare_duration(duration_limit_ms, elapsed_ms)
                self._record_result(
                    vector_file=vector_name,
                    test_type=TestType.PARSE,
                    test_name=name,
                    adapter=adapter.name,
                    passed=passed,
                    description=description,
                    tags=tags,
                    spec_ref=spec_ref,
                    expected=expected,
                    actual=result,
                    error=error,
                )

            # Format test
            if tests.get("format") is True and format_cmd and obj is not None:
                if is_base64url:
                    format_input = obj
                else:
                    format_input = json.dumps(obj)
                result, elapsed_ms = self.run_adapter_timed(adapter, format_cmd, format_input, timeout=command_timeout)
                expected_format = {"success": True, "result": wire}
                passed, error = self.compare_format_results_semantic(adapter, expected_format, result, format_cmd, parse_cmd)
                if passed:
                    passed, error = self.compare_duration(duration_limit_ms, elapsed_ms)
                self._record_result(
                    vector_file=vector_name,
                    test_type=TestType.FORMAT,
                    test_name=name,
                    adapter=adapter.name,
                    passed=passed,
                    description=description,
                    tags=tags,
                    spec_ref=spec_ref,
                    expected=expected_format,
                    actual=result,
                    error=error,
                )

            # Roundtrip test
            if tests.get("roundtrip") is True and parse_cmd and format_cmd and obj is not None:
                if is_base64url:
                    format_input = obj
                else:
                    format_input = json.dumps(obj)
                format_result = self.run_adapter(adapter, format_cmd, format_input, timeout=command_timeout)

                if not format_result.get("success"):
                    self._record_result(
                        vector_file=vector_name,
                        test_type=TestType.ROUNDTRIP,
                        test_name=name,
                        adapter=adapter.name,
                        passed=False,
                        description=description,
                        tags=tags,
                        spec_ref=spec_ref,
                        expected=obj,
                        actual=format_result,
                        error=f"format failed: {format_result.get('error')}",
                    )
                    continue

                formatted = format_result["result"]
                parse_result = self.run_adapter(adapter, parse_cmd, formatted, timeout=command_timeout)

                if not parse_result.get("success"):
                    self._record_result(
                        vector_file=vector_name,
                        test_type=TestType.ROUNDTRIP,
                        test_name=name,
                        adapter=adapter.name,
                        passed=False,
                        description=description,
                        tags=tags,
                        spec_ref=spec_ref,
                        expected=obj,
                        actual=parse_result,
                        error=f"parse failed: {parse_result.get('error')}",
                    )
                    continue

                parsed = parse_result["result"]
                if parse_cmd == "parse-authorization":
                    parsed = self.normalize_credential_result(parsed)
                    obj_normalized = self.normalize_credential_result(obj)
                else:
                    obj_normalized = obj

                if parsed == obj_normalized:
                    passed = True
                    error = None
                else:
                    passed = False
                    error = format_mismatch_error(obj_normalized, parsed, "roundtrip")
                self._record_result(
                    vector_file=vector_name,
                    test_type=TestType.ROUNDTRIP,
                    test_name=name,
                    adapter=adapter.name,
                    passed=passed,
                    description=description,
                    tags=tags,
                    spec_ref=spec_ref,
                    expected=obj_normalized,
                    actual=parsed,
                    error=error,
                )

    def run(
        self,
        adapter_names: list[str] | None = None,
        vector_names: list[str] | None = None,
        tag_filter: str | None = None,
    ) -> bool:
        """Run all conformance tests."""
        adapters = discover_adapters()
        if adapter_names is None:
            adapter_names = list(adapters.keys())
        
        # Auto-discover vector files
        all_vectors = discover_vector_files()
        if vector_names is None:
            vector_names = sorted(all_vectors.keys())

        self.log("=" * 60)
        self.log("MPP Conformance Test Runner")
        self.log("=" * 60)

        for adapter_name in adapter_names:
            if adapter_name not in adapters:
                self.log(f"Unknown adapter: {adapter_name}")
                self._record_result(
                    vector_file="adapter",
                    test_type=TestType.BUILD,
                    test_name="unknown_adapter",
                    adapter=adapter_name,
                    passed=False,
                    description="Requested adapter is registered before running conformance vectors",
                    expected=f"one of {sorted(adapters.keys())}",
                    actual=adapter_name,
                    error=f"Unknown adapter: {adapter_name}",
                )
                continue

            adapter = adapters[adapter_name]
            self.log(f"\n{adapter.name}", flush=True)

            # Build if needed
            if adapter.build_command:
                self.log(f"  building...", end="", flush=True)
                build_error = build_adapter(adapter)
                if build_error:
                    self.log(f" FAILED")
                    self._record_result(
                        vector_file="adapter",
                        test_type=TestType.BUILD,
                        test_name="build",
                        adapter=adapter.name,
                        passed=False,
                        description="Adapter builds successfully before running conformance vectors",
                        expected="adapter builds successfully",
                        actual=None,
                        error=build_error,
                    )
                    continue
                self.log(f" ok")
            else:
                build_error = build_adapter(adapter)
                if build_error:
                    self.log(f"  build FAILED")
                    self._record_result(
                        vector_file="adapter",
                        test_type=TestType.BUILD,
                        test_name="build",
                        adapter=adapter.name,
                        passed=False,
                        description="Adapter builds successfully before running conformance vectors",
                        expected="adapter builds successfully",
                        actual=None,
                        error=build_error,
                    )
                    continue

            for vector_name in vector_names:
                if vector_name not in all_vectors:
                    self.log(f"  {vector_name}: SKIPPED (not found)")
                    continue
                before_count = len(self.results)
                self.run_vector_file(adapter, all_vectors[vector_name], tag_filter=tag_filter)
                # Print results for this vector file
                for r in self.results[before_count:]:
                    status = "PASS" if r.passed else "FAIL"
                    self.log(f"  {r.vector_file}::{r.test_type}::{r.test_name} ... {status}")
                    if not r.passed and r.error and self.verbose:
                        for line in r.error.split("\n"):
                            self.log(f"    {line}")

        if not self.results:
            self._record_result(
                vector_file="runner",
                test_type=TestType.BUILD,
                test_name="no_checks",
                adapter="runner",
                passed=False,
                description="Runner executes at least one conformance check",
                expected="at least one conformance check",
                actual=0,
                error="No conformance checks were executed",
            )

        # Compute summary
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        total = len(self.results)

        if self.output_format == "json":
            self._output_json(passed, failed, total)
        else:
            self._output_text(passed, failed, total)

        return failed == 0

    def _output_json(self, passed: int, failed: int, total: int) -> None:
        """Output results in JSON format."""
        errors = []
        for r in self.results:
            if not r.passed:
                errors.append({
                    "adapter": r.adapter,
                    "vector": r.vector_file,
                    "test_type": r.test_type,
                    "test_name": r.test_name,
                    "error": r.error,
                    "expected": r.expected,
                    "actual": r.actual,
                })
        
        output = {
            "status": "pass" if failed == 0 else "fail",
            "num_checks": total,
            "passed": passed,
            "failed": failed,
            "checks": [r.to_check() for r in self.results],
            "errors": errors,
        }
        print(json.dumps(output, indent=2))

    def _output_text(self, passed: int, failed: int, total: int) -> None:
        """Output results in text format."""
        self.log("")
        self.log("-" * 60)

        if failed > 0:
            self.log(f"FAILED: {passed} passed, {failed} failed, {total} total")
            self.log("")
            self.log("Failures:")
            self.log("")
            failure_num = 0
            for r in self.results:
                if not r.passed:
                    failure_num += 1
                    self.log(f"  {failure_num}) {r.adapter}::{r.vector_file}::{r.test_type}::{r.test_name}")
                    if r.error:
                        for line in r.error.split("\n"):
                            self.log(f"       {line}")
                    self.log("")
        else:
            self.log(f"PASSED: {passed} passed, {total} total")


def main():
    parser = argparse.ArgumentParser(description="Run conformance tests for MPP tooling")
    parser.add_argument(
        "--adapter",
        type=str,
        action="append",
        dest="adapters",
        help="Run only specified adapter(s) - can be used multiple times (typescript, go, rust, python, ruby, java)",
    )
    parser.add_argument(
        "--vector",
        type=str,
        help="Run only the specified vector (www-authenticate, authorization, receipt, base64url)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output for each test",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        choices=["text", "json"],
        default="text",
        help="Output format: text (default) or json",
    )
    parser.add_argument(
        "--tag",
        type=str,
        help="Run only scenarios with the specified tag",
    )
    args = parser.parse_args()

    adapter_names = args.adapters if args.adapters else None
    vector_names = [args.vector] if args.vector else None

    runner = VectorRunner(verbose=args.verbose, output_format=args.output)
    success = runner.run(adapter_names=adapter_names, vector_names=vector_names, tag_filter=args.tag)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
