from __future__ import annotations

import json
from difflib import unified_diff
from typing import Any


def _normalized(value: Any, *, ignore_order: bool) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalized(item, ignore_order=ignore_order)
            for key, item in value.items()
        }
    if isinstance(value, list):
        items = [_normalized(item, ignore_order=ignore_order) for item in value]
        if ignore_order:
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return items
    return value


def json_diff(expected: Any, actual: Any, *, ignore_order: bool = False) -> str:
    expected = _normalized(expected, ignore_order=ignore_order)
    actual = _normalized(actual, ignore_order=ignore_order)
    if expected == actual:
        return ""

    expected_lines = json.dumps(expected, indent=2, sort_keys=True, default=str).splitlines()
    actual_lines = json.dumps(actual, indent=2, sort_keys=True, default=str).splitlines()
    return "\n".join(
        unified_diff(
            expected_lines,
            actual_lines,
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )


def format_json_mismatch(
    expected: Any,
    actual: Any,
    label: str = "result",
    *,
    ignore_order: bool = False,
) -> str:
    diff = json_diff(expected, actual, ignore_order=ignore_order)
    return f"{label} mismatch:\n{diff}" if diff else ""
