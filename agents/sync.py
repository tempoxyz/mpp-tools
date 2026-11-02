"""Keep every MPP repository's AGENTS.md in one canonical shape.

The shared section (agents/shared.md) is wrapped in markers and kept last in
each AGENTS.md; everything above it belongs to the repository. Commands:

  sync.py render PATH   write the current shared section into PATH
  sync.py check PATH... report shape errors (exit 1 when there are any)
  sync.py audit         check every repository in agents/repos.json on main
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE / "shared.md"
REPOS = HERE / "repos.json"

BEGIN = (
    "<!-- BEGIN MPP SHARED: synced from tempoxyz/mpp-tools agents/shared.md. "
    "Edit it there; changes here are overwritten. -->"
)
END = "<!-- END MPP SHARED -->"
REQUIRED = ("Commands", "Layout", "Conventions")

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_BLOCK = re.compile(
    re.escape("<!-- BEGIN MPP SHARED") + r".*?" + re.escape(END) + r"\n?",
    re.DOTALL,
)


def block(shared: str) -> str:
    return f"{BEGIN}\n\n{shared.strip()}\n\n{END}\n"


def render(doc: str, shared: str) -> str:
    """Return doc with the current shared section replacing or appended to its own."""
    new = block(shared)
    start = doc.find("<!-- BEGIN MPP SHARED")
    if start != -1 and doc.find(END, start) == -1:
        raise ValueError("the shared MPP section has no end marker; fix it by hand")
    if _BLOCK.search(doc):
        head = _BLOCK.split(doc, maxsplit=1)
        return head[0].rstrip("\n") + "\n\n" + new + head[1].lstrip("\n")
    return doc.rstrip("\n") + "\n\n" + new


def _headings(text: str) -> list[tuple[int, str]]:
    """Markdown ATX headings outside fenced code, as (level, title)."""
    found: list[tuple[int, str]] = []
    fence = None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if h:
            found.append((len(h.group(1)), h.group(2)))
    return found


def check(doc: str, shared: str) -> list[str]:
    """Shape errors in an AGENTS.md; empty when it matches the canonical shape."""
    errors: list[str] = []
    starts = doc.count("<!-- BEGIN MPP SHARED")
    if starts == 0:
        errors.append("missing the shared MPP section (run sync.py render)")
        own = doc
    elif starts > 1:
        return ["the shared MPP section appears more than once"]
    else:
        own, rest = doc.split("<!-- BEGIN MPP SHARED", 1)
        current = "<!-- BEGIN MPP SHARED" + rest
        if END not in current:
            errors.append("the shared MPP section has no end marker")
        elif current.split(END, 1)[1].strip():
            errors.append("the shared MPP section must be the last thing in the file")
        if current.rstrip("\n") != block(shared).rstrip("\n"):
            errors.append("the shared MPP section is out of date (run sync.py render)")

    heads = _headings(own)
    first = next((ln for ln in own.splitlines() if ln.strip()), "")
    if not first.startswith("# "):
        errors.append("must start with a '# <repository>' title")
    elif sum(1 for level, _ in heads if level == 1) > 1:
        errors.append("must have exactly one '# ' title outside code blocks")
    else:
        intro = own.split("\n## ", 1)[0]
        title_seen = False
        body = []
        for ln in intro.splitlines():
            if not title_seen:
                title_seen = ln.startswith("# ")
                continue
            if ln.strip() and not ln.lstrip().startswith("<"):
                body.append(ln)
        if not body:
            errors.append(
                "needs a short paragraph under the title saying what the repository is"
            )

    h2 = [title for level, title in heads if level == 2]
    if tuple(h2[: len(REQUIRED)]) != REQUIRED:
        errors.append(
            "the first sections must be "
            + ", ".join(f"'## {t}'" for t in REQUIRED)
            + f" in that order (found: {', '.join(h2[:3]) or 'none'})"
        )
    if "MPP" in h2:
        errors.append("'## MPP' is reserved for the shared section")
    return errors


def _fetch(repo: str, path: str = "AGENTS.md", ref: str = "main") -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https host)
            return resp.read().decode()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--shared",
        type=Path,
        default=SHARED,
        help="shared section (default: agents/shared.md)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_render = sub.add_parser(
        "render", help="write the shared section into an AGENTS.md"
    )
    p_render.add_argument("path", type=Path)
    p_check = sub.add_parser("check", help="report shape errors")
    p_check.add_argument("paths", type=Path, nargs="+")
    sub.add_parser(
        "audit", help="check every repository in repos.json on its default branch"
    )
    args = parser.parse_args(argv)
    shared = args.shared.read_text()

    if args.command == "render":
        doc = args.path.read_text() if args.path.exists() else ""
        try:
            new = render(doc, shared)
        except ValueError as err:
            print(f"{args.path}: {err}", file=sys.stderr)
            return 1
        if new != doc:
            args.path.write_text(new)
            print(f"{args.path}: updated")
        else:
            print(f"{args.path}: up to date")
        return 0

    if args.command == "check":
        failed = False
        for path in args.paths:
            for error in check(path.read_text(), shared):
                failed = True
                print(f"{path}: {error}")
        return 1 if failed else 0

    repos = json.loads(REPOS.read_text())["repositories"]
    failed = False
    print("| Repository | AGENTS.md |")
    print("| --- | --- |")
    for entry in repos:
        if entry.get("private"):
            # Anonymous raw fetches can't read private repositories.
            print(f"| {entry['repo']} | skipped (private) |")
            continue
        try:
            doc = _fetch(entry["repo"])
        except (urllib.error.URLError, TimeoutError) as err:
            errors = [f"could not fetch AGENTS.md: {err}"]
        else:
            errors = ["no AGENTS.md on main"] if doc is None else check(doc, shared)
        failed = failed or bool(errors)
        print(f"| {entry['repo']} | {'ok' if not errors else '; '.join(errors)} |")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
