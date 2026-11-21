#!/usr/bin/env python3
"""Keep the one abnf-fuzz-disagreement issue in step with the nightly ABNF fuzzing run.

The ABNF Differential Fuzzing workflow runs abnf_fuzz.py in a job without
write access and uploads its JSON report. A separate job passes that report
here; this script runs no SDK or adapter code and treats the report as data.

The issue body holds the current findings: counts per adapter and kind, one
line per finding, and the finding keys in a hidden marker. A run with the same
findings only refreshes the body. A run with new or resolved findings also
comments with just those. The issue closes once nothing disagrees. The exit
status is 1 when the run finds something the issue did not already list, an
adapter did not start, or the run failed before reporting, so the nightly run
is red only on changes.

Environment: REPORT (path to abnf-fuzz-report.json, may be missing), STATUS
(abnf_fuzz.py's exit status), RUN_URL, and GH_TOKEN and GH_REPO for gh.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LABEL = "abnf-fuzz-disagreement"
LABEL_COLOR = "B60205"
LABEL_DESCRIPTION = "Adapters disagree with the spec ABNF"
TITLE = "Adapters disagree with the spec ABNF"
MARKER = re.compile(r"<!-- abnf-fuzz-findings:(?P<data>[A-Za-z0-9+/=]*) -->")
# Keeps bodies well under GitHub's 65536-character limit.
MAX_LINES = 200
REFERENCE = "mppx"


def _text(value: Any, limit: int = 80) -> str:
    """A report value as one short line of Markdown-safe text (the report is untrusted)."""
    text = re.sub(r"[^\w .,:;()/+*=#-]", "?", str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Finding:
    """One finding group from the report, keyed the way abnf_fuzz.py groups it."""

    source: str
    adapter: str
    operation: str
    kind: str
    fields: tuple[str, ...]
    mutations: tuple[str, ...]

    @property
    def key(self) -> str:
        what = self.kind + (f" ({', '.join(self.fields)})" if self.fields else "")
        # Verdict disagreements group by mutation; value and reference ones do not.
        grouped = self.source == "grammar" and self.kind != "value"
        mutation = f" [{', '.join(self.mutations)}]" if grouped else ""
        against = "grammar" if self.source == "grammar" else REFERENCE
        return f"{against}: {self.adapter} {self.operation} {what}{mutation}"


def findings(report: dict[str, Any]) -> list[Finding]:
    """The report's grammar disagreements and its reference divergences outside the allowlist."""
    rows = [("grammar", row) for row in report.get("grammarDisagreements") or []]
    rows += [("reference", row) for row in report.get("referenceDivergences") or [] if isinstance(row, dict) and not row.get("allowlisted")]
    out: list[Finding] = []
    for source, row in rows:
        if not isinstance(row, dict):
            continue
        mutations = row.get("mutations") if isinstance(row.get("mutations"), dict) else {}
        fields = row.get("fields") if isinstance(row.get("fields"), list) else []
        out.append(Finding(
            source,
            _text(row.get("adapter"), 30),
            _text(row.get("operation"), 40),
            _text(row.get("kind"), 30),
            tuple(_text(name) for name in fields),
            tuple(sorted(_text(name, 40) for name in mutations)),
        ))
    # One entry per key, as the issue tracks them.
    unique = {finding.key: finding for finding in out}
    return [unique[key] for key in sorted(unique)]


def encode_keys(keys: list[str]) -> str:
    return base64.b64encode(json.dumps(sorted(keys)).encode("utf-8")).decode("ascii")


def decode_keys(body: str) -> set[str] | None:
    """The finding keys an issue body recorded, or None without a readable marker."""
    match = MARKER.search(body or "")
    if not match:
        return None
    try:
        keys = json.loads(base64.b64decode(match["data"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return {key for key in keys if isinstance(key, str)} if isinstance(keys, list) else None


def _bullets(keys: list[str]) -> list[str]:
    lines = [f"- {key}" for key in keys[:MAX_LINES]]
    if len(keys) > MAX_LINES:
        lines.append(f"- … and {len(keys) - MAX_LINES} more (see the run's report artifact)")
    return lines


def issue_body(report: dict[str, Any], current: list[Finding], run_url: str) -> str:
    """The issue body: what the latest nightly run found, with its keys in a marker."""
    by_kind: Counter[tuple[str, str]] = Counter()
    for finding in current:
        by_kind[(finding.adapter, finding.kind if finding.source == "grammar" else f"{finding.kind} vs {REFERENCE}")] += 1
    lines = [
        (
            f"The nightly ABNF differential fuzzing run (seed {_text(report.get('seed'))}, "
            f"{_text(report.get('casesPerKind'))} cases per header kind, grammar from {_text(str(report.get('specs')).replace('@', ' at '))}) "
            f"finds {len(current)} disagreements."
        ),
        "",
        f"**Latest run:** {run_url}",
        "",
    ]
    build_errors = report.get("buildErrors") if isinstance(report.get("buildErrors"), dict) else {}
    for name, error in sorted(build_errors.items()):
        lines += [f"Adapter {_text(name, 30)} did not start: {_text(error, 300)}", ""]
    if by_kind:
        lines += ["| Adapter | Disagreement | Findings |", "| --- | --- | --- |"]
        lines += [f"| {adapter} | {kind} | {count} |" for (adapter, kind), count in sorted(by_kind.items())]
        lines += ["", "### Findings", ""]
        lines += _bullets([finding.key for finding in current])
    lines += [
        "",
        (
            "A run with new or resolved findings comments with just those; the run's step summary and report "
            "artifact hold an example input for each. This issue closes once adapters agree with the grammar again."
        ),
        "",
        f"<!-- abnf-fuzz-findings:{encode_keys([finding.key for finding in current])} -->",
    ]
    return "\n".join(lines) + "\n"


def change_comment(new: list[str], resolved: list[str], total: int, run_url: str) -> str:
    lines = [f"{len(new)} new and {len(resolved)} resolved findings, {total} in all. Run: {run_url}"]
    if new:
        lines += ["", "**New**", "", *_bullets(new)]
    if resolved:
        lines += ["", "**Resolved**", "", *_bullets(resolved)]
    return "\n".join(lines) + "\n"


@dataclass
class Plan:
    """What to do with the issue, and whether the run fails."""

    create: str | None = None  # body of a new issue
    edit: str | None = None  # new body of the open issue
    comment: str | None = None
    close: str | None = None  # closing comment
    failed: bool = False


def plan(report: dict[str, Any] | None, status: str, existing_body: str | None, run_url: str) -> Plan:
    """Decide the issue update from the run's report and the open issue's body (None when none is open)."""
    if report is None or status not in ("0", "1"):
        message = f"ABNF differential fuzzing failed before writing a report (exit status {_text(status or 'unknown')}). Run: {run_url}\n"
        return Plan(create=message, failed=True) if existing_body is None else Plan(comment=message, failed=True)
    current = findings(report)
    build_errors = bool(report.get("buildErrors"))
    if not current and not build_errors:
        return Plan() if existing_body is None else Plan(close=f"Adapters agree with the ABNF again: {run_url}")
    body = issue_body(report, current, run_url)
    if existing_body is None:
        return Plan(create=body, failed=True)
    keys = {finding.key for finding in current}
    previous = decode_keys(existing_body) or set()
    new = sorted(keys - previous)
    resolved = sorted(previous - keys)
    comment = change_comment(new, resolved, len(keys), run_url) if new or resolved else None
    return Plan(edit=body, comment=comment, failed=bool(new) or build_errors)


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def load_report(path: str) -> dict[str, Any] | None:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def main() -> int:
    report = load_report(os.environ.get("REPORT", ""))
    run_url = os.environ["RUN_URL"]
    open_issues = json.loads(gh("issue", "list", "--label", LABEL, "--state", "open", "--limit", "1", "--json", "number,body"))
    number = str(open_issues[0]["number"]) if open_issues else None
    action = plan(report, os.environ.get("STATUS", ""), open_issues[0]["body"] if open_issues else None, run_url)
    if action.create is not None:
        gh("label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force")
        print(gh("issue", "create", "--title", TITLE, "--body", action.create, "--label", LABEL).strip())
    if number and action.edit is not None:
        gh("issue", "edit", number, "--body", action.edit)
    if number and action.comment is not None:
        gh("issue", "comment", number, "--body", action.comment)
    if number and action.close is not None:
        gh("issue", "close", number, "--comment", action.close)
    if action.failed:
        print("ABNF fuzzing found disagreements the issue did not list, or did not finish; see the issue.", file=sys.stderr)
    return 1 if action.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
