#!/usr/bin/env python3
"""Report which BCP 14 requirements in the MPP specifications conformance covers.

Reads every `specs/**/*.md` draft in a tempoxyz/mpp-specs checkout (a fresh
clone of main when --specs is omitted) and extracts each paragraph or list item
that contains an RFC 2119/8174 keyword in capitals. A requirement's key is the
anchor a `{: #req-<draft>-<nnn>}` IAL gives it when present, otherwise
`<draft>#<section>:<sha256 prefix>`. Vector scenarios and flow cases claim
requirement IDs with an optional `requirements` array; adapter manifests claim
drafts with an optional `profile`. The report lists, per draft, how many rules
there are, how many carry an ID and how many a vector or flow covers.

Drafts are read from the XML kramdown-rfc renders them to, not from their Markdown:
kramdown-rfc decides what is a paragraph, a list item, a table, code or a comment,
and which element an IAL tags, exactly as it does for the published draft. Reading the
Markdown by hand had to re-implement kramdown's grammar (IALs and attribute list
definitions, reference links, comments, code spans and blocks, tables, setext
headings, line splitting) and kept getting corners of it wrong.

The trade-off is a dependency: kramdown-rfc (a Ruby gem) must be installed, at the
version the checkout's Gemfile.lock pins (KRAMDOWN_RFC_VERSION when it has none), since
another version may render, and so count, the drafts differently. The report checks
that version before it reads any draft and fails with an error naming the fix when the
command is missing or another version answers; rendering through Docker (mpp-specs'
ietf-spec-tools image) or with RubyGems' `kramdown-rfc _<version>_` selector works
through --kramdown-rfc. Rendering costs about a second per draft, so the drafts are
rendered in parallel. The rendered XML carries no source line numbers below the section
level, so a rule's line is found by matching its words against the Markdown; it is
only used to point a reader at the rule.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from harness import (
    ADAPTERS_DIR,
    COMMAND_TO_OPERATION,
    CONFORMANCE_DIR,
    FLOW_CASES_PATH,
    VECTORS_DIR,
    load_flow_cases,
    load_flow_results,
    load_json,
    load_vector,
    request_input_for_command,
    validate_value,
)
from vector_runner import VectorRunner, expects_failure
from version_constraints import matches_constraint

SPECS_REPOSITORY = "https://github.com/tempoxyz/mpp-specs"
EXCERPT_LENGTH = 160

# The kramdown-rfc version mpp-specs renders its drafts with (its Gemfile.lock at the
# time of writing). Used when a checkout has no Gemfile.lock pinning one; the rendered
# fixtures in testdata/ were made with it.
KRAMDOWN_RFC_VERSION = "1.7.39"
# The resolved gem in a Gemfile.lock's GEM specs, indented four spaces; the two-space
# DEPENDENCIES and CHECKSUMS entries name the same version or a constraint.
GEMFILE_LOCK_RE = re.compile(r"^    kramdown-rfc \(([^()\s]+)\)$", re.MULTILINE)
# 'kramdown-rfc --version' prints 'kramdown-rfc 1.7.39'.
KRAMDOWN_VERSION_RE = re.compile(r"^kramdown-rfc (\S+)\s*$", re.MULTILINE)
# How long one draft may take to render; real drafts take about a second.
RENDER_TIMEOUT_SECONDS = 300
# Environment for every render. KRAMDOWN_SAFE turns off {::include}, so a draft cannot
# copy other files on this machine into the report (no draft includes one; the
# published build would differ if one did, and that draft's rules would be missed).
# KRAMDOWN_NO_SOURCE leaves out the gzipped copy of the Markdown kramdown-rfc appends,
# which the report never reads, and KRAMDOWN_OFFLINE keeps reference lookups off the
# network (a non-standalone render only declares references as entities anyway).
RENDER_ENV = {"KRAMDOWN_SAFE": "1", "KRAMDOWN_NO_SOURCE": "1", "KRAMDOWN_OFFLINE": "1"}

# Longest alternatives first so "MUST NOT" is not read as "MUST". keywords_in collapses
# whitespace before matching, so a two-word keyword wrapped across lines ("MUST" at the
# end of one line, "NOT" at the start of the next) still matches as "MUST NOT".
# Matched on the rendered text rather than read from kramdown-rfc's <bcp14> tags: it
# tags keywords inside autolinks and leaves a keyword written with character references
# ('&#77;UST') untagged, while readers see the text, and mpp-specs' linter matches it.
KEYWORD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:MUST NOT|SHALL NOT|SHOULD NOT|NOT RECOMMENDED|MUST|REQUIRED|SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)(?![A-Za-z0-9])"
)
# A keyword quoted on its own, as in the boilerplate 'The key words "MUST", ...', names
# the keyword rather than stating a rule. Only that exact form is dropped, the way
# mpp-specs' scripts/lint_requirements.py does (QUOTED_KEYWORDS), so both tools agree on
# which blocks are rules: a keyword in a longer quoted phrase ("clients MUST retry") or
# in single quotes still counts. kramdown-rfc keeps straight quotes as they are.
QUOTED_KEYWORD_RE = re.compile(
    r'"(?:MUST NOT|SHALL NOT|SHOULD NOT|NOT RECOMMENDED|MUST|REQUIRED|SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)"'
)
# req-<draft>-<nnn>, where <draft> is a short draft name: lowercase words joined by single
# hyphens, so an empty component such as req-core--001 is malformed. The schemas use the
# same pattern.
REQ_ID_RE = re.compile(r"^req-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{3}$")
# An IAL kramdown-rfc left in the text: one at the end of a line of text (a span IAL
# after text, which kramdown ignores with a warning) renders literally. The ID it meant
# to set: a '#req-...' name or a quoted id="req-..." value.
LITERAL_IAL_RE = re.compile(r"\{:[^{}]*\}")
LITERAL_IAL_ID_RE = re.compile(r"""(?:(?<=[\s:])#|\bid=["'])(req-[\w-]*)""")
# A word as the line locator compares them: rendering drops Markdown punctuation but
# keeps every letter and digit run of the text in order.
WORD_RE = re.compile(r"[A-Za-z0-9]+")
# How many consecutive words of a rule must match the Markdown to place it: enough that
# rules opening with the same phrase ("Servers MUST verify that the ...") are told apart.
LOCATE_WINDOW = 12
# Candidate positions SourceLines.find compares per call before giving up.
LOCATE_BUDGET = 4096

# Elements a rule is read from: paragraphs (<t>, also list items in kramdown-rfc's
# default vocabulary, where a list is <list> holding <t> items), v3 list items and
# definition list terms and definitions. Their own inline text is the rule; block
# elements inside them are read on their own.
RULE_TAGS = frozenset({"t", "li", "dt", "dd"})
# Elements that start a block of their own inside a paragraph or item: their text is not
# part of the enclosing rule.
BLOCK_TAGS = frozenset(
    {
        "t",
        "list",
        "ul",
        "ol",
        "dl",
        "li",
        "dt",
        "dd",
        "figure",
        "artwork",
        "artset",
        "sourcecode",
        "table",
        "texttable",
        "blockquote",
        "aside",
    }
)
# Code and art are never prose. A figure holds code or art (its name is a caption).
CODE_TAGS = frozenset({"figure", "artwork", "artset", "sourcecode"})
TABLE_TAGS = frozenset({"table", "texttable"})
# Inline elements whose text is never read: an index entry renders nothing, and a cref
# is an editor's comment, not part of the draft's text.
HIDDEN_INLINE_TAGS = frozenset({"iref", "cref"})
# Inline elements: an anchor on one (a span IAL) tags a span, not a rule.
SPAN_TAGS = frozenset(
    {"em", "strong", "bcp14", "sub", "sup", "tt", "spanx", "eref", "xref", "u"}
)
# Front matter elements other than these (title, authors, dates) hold no rules.
FRONT_RULE_TAGS = frozenset({"abstract", "note"})
# What a misplaced requirement anchor went on, for the warning.
ANCHOR_PLACES = {
    "section": "a heading",
    "list": "a list",
    "ul": "a list",
    "ol": "a list",
    "dl": "a list",
    "table": "a table",
    "texttable": "a table",
    "figure": "a code block or figure",
    "artwork": "a code block or figure",
    "artset": "a code block or figure",
    "sourcecode": "a code block or figure",
    "blockquote": "a quote",
    "aside": "an aside",
}


@dataclass
class Requirement:
    """One BCP 14 rule: a paragraph or list item with a keyword in capitals."""

    draft: str
    file: str
    line: int
    section: str
    text: str
    req_id: str | None = None
    fallback_key: str = ""
    keywords: list[str] = field(default_factory=list)
    # Why no claim may cover this rule through its ID: "duplicate" when another rule
    # carries the same ID, "foreign" when the ID has another draft's prefix. Coverage is
    # matched by ID, so without this one claim on a shared ID would mark every rule that
    # carries it covered (and a mis-prefixed rule would be credited by another draft's
    # tests). "malformed" when the ID does not match req-<draft>-<nnn>: no claim can cite
    # it, so it counts as neither tagged nor covered. Such a rule counts as uncovered
    # until its ID is fixed.
    id_problem: str | None = None

    @property
    def key(self) -> str:
        return self.req_id or self.fallback_key


def draft_short_name(docname: str) -> str:
    """Map a draft name to the short name requirement IDs use, such as tempo-charge."""
    name = re.sub(r"-\d{2}$", "", docname.removesuffix(".md"))
    name = name.removeprefix("draft-")
    if name == "httpauth-payment":
        return "core"
    return name.removeprefix("payment-")


def slugify(text: str) -> str:
    """Slug a title the way kramdown derives an anchor from a heading."""
    text = re.sub(r"[\x60*_]", "", text).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "section"


def normalize(text: str) -> str:
    """Collapse whitespace so reflowing a rule keeps its hash."""
    return " ".join(text.split())


def keywords_in(prose: str) -> list[str]:
    """Return the BCP 14 keywords in a rule's prose, its text without code or link targets.

    A keyword double-quoted on its own ("MUST", see QUOTED_KEYWORD_RE) is ignored.
    Whitespace, newlines included, is collapsed first so a keyword pair wrapped across
    lines still reads as one keyword ("MUST NOT", not "MUST").
    """
    visible = QUOTED_KEYWORD_RE.sub(" ", normalize(prose))
    return sorted(set(KEYWORD_RE.findall(visible)))


def locked_kramdown_rfc_version(specs_dir: Path) -> str:
    """Return the kramdown-rfc version the checkout renders its drafts with.

    That is the version its Gemfile.lock resolves (mpp-specs' Dockerfile installs from
    it), or KRAMDOWN_RFC_VERSION for a checkout without one.
    """
    lock = specs_dir / "Gemfile.lock"
    try:
        text = lock.read_text(encoding="utf-8")
    except OSError:
        return KRAMDOWN_RFC_VERSION
    match = GEMFILE_LOCK_RE.search(text)
    return match.group(1) if match else KRAMDOWN_RFC_VERSION


class KramdownRfc:
    """Renders drafts to XML with a kramdown-rfc command, after checking its version.

    command is the argv prefix to run, such as ['kramdown-rfc'], ['kramdown-rfc',
    '_1.7.39_'] (RubyGems picks that installed version) or a 'docker run -i' line: the
    draft is sent on stdin, which renders exactly as a file argument does, so the
    command needs no access to the checkout.
    """

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self.explicit = command is not None
        self.command = list(command) if command else ["kramdown-rfc"]

    def version(self, command: Sequence[str] | None = None) -> str:
        """Return the version the command reports, or raise ValueError when it cannot run."""
        argv = [*(command or self.command), "--version"]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=RENDER_TIMEOUT_SECONDS
            )
        except FileNotFoundError:
            raise ValueError(
                f"{argv[0]} not found: the gap report renders drafts with kramdown-rfc. "
                f"Install it (gem install kramdown-rfc -v {KRAMDOWN_RFC_VERSION}) or pass "
                "--kramdown-rfc with a command that runs it"
            ) from None
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"cannot run {shlex.join(argv)}: {error}") from None
        match = KRAMDOWN_VERSION_RE.search(result.stdout)
        if result.returncode != 0 or not match:
            raise ValueError(
                f"{shlex.join(argv)} did not report a kramdown-rfc version "
                f"(exit {result.returncode})"
            )
        return match.group(1)

    def require(self, version: str) -> None:
        """Make sure the command is kramdown-rfc at exactly version, or raise ValueError.

        When the default command answers with another version, RubyGems' selector
        ('kramdown-rfc _<version>_') is tried, so a machine with several versions
        installed needs no flag. An explicit --kramdown-rfc command is used as given.
        """
        found = self.version()
        if found == version:
            return
        if not self.explicit:
            selected = [*self.command, f"_{version}_"]
            try:
                if self.version(selected) == version:
                    self.command = selected
                    return
            except ValueError:
                pass
        raise ValueError(
            f"{shlex.join(self.command)} is kramdown-rfc {found}, but the drafts are rendered "
            f"with {version} (mpp-specs' Gemfile.lock); install it (gem install kramdown-rfc "
            f"-v {version}) or pass --kramdown-rfc with a command that runs it, such as "
            f"'docker run --rm -i ietf-spec-tools kramdown-rfc'"
        )

    def render_command(self) -> list[str]:
        """Return the argv a render runs: the command, passing RENDER_ENV into a container.

        A container does not inherit the environment of the docker (or podman) CLI that
        starts it, so for a 'docker run' line each RENDER_ENV variable is forwarded with
        '-e NAME' right after 'run'. Without it a containerized kramdown-rfc would render
        with {::include} on and network lookups allowed.
        """
        command = list(self.command)
        if (
            len(command) >= 2
            and Path(command[0]).name in {"docker", "podman"}
            and command[1] == "run"
        ):
            forwarded = [flag for name in RENDER_ENV for flag in ("-e", name)]
            return [*command[:2], *forwarded, *command[2:]]
        return command

    def __call__(self, markdown: bytes) -> str:
        """Return the XML kramdown-rfc renders markdown to, or raise ValueError."""
        try:
            result = subprocess.run(
                self.render_command(),
                input=markdown,
                capture_output=True,
                timeout=RENDER_TIMEOUT_SECONDS,
                env={**os.environ, **RENDER_ENV},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(
                f"cannot run {shlex.join(self.command)}: {error}"
            ) from None
        if result.returncode != 0:
            # kramdown-rfc's messages quote the draft; keep only the last line, bounded.
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            detail = message[-1][:200] if message else "no message"
            raise ValueError(
                f"kramdown-rfc failed (exit {result.returncode}): {detail}"
            )
        return result.stdout.decode("utf-8")


# Characters XML 1.0 does not allow, which kramdown-rfc copies from the Markdown.
XML_INVALID_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
# The entities kramdown-rfc's DOCTYPE declares, for a fragment rendered without one.
FRAGMENT_PROLOG = (
    '<!DOCTYPE rfc [<!ENTITY nbsp "&#160;"><!ENTITY zwsp "&#8203;">'
    '<!ENTITY nbhy "&#8209;"><!ENTITY wj "&#8288;">]>'
)
# A requirement ID an IAL or attribute list definition in the Markdown sets: a
# '#req-...' name or a quoted id="req-..." value. Only used to find IDs that never reach
# the rendering; which block each one tags is kramdown-rfc's to decide.
SOURCE_ID_RE = re.compile(r"""(?:(?<![\w&-])#|\bid=["'])(req-[A-Za-z0-9_-]+)""")

# Renders one draft's Markdown bytes to kramdown-rfc XML.
Renderer = Callable[[bytes], str]


def parse_xml(document: str, comments: list[str] | None = None) -> ET.Element:
    """Parse kramdown-rfc XML into an element tree, keeping its processing instructions.

    kramdown-rfc declares each reference as an external entity in its DOCTYPE and uses
    it in the references section (&RFC9110;). ElementTree's own parser fails on those,
    so expat is driven directly and told to skip every external entity rather than
    fetch it; parameter entities are never read. The <?line N?> instructions are kept:
    they are the only source line numbers in the output. The text of XML comments (the
    draft's HTML comments) goes to comments when given.

    Two repairs keep a broken draft readable rather than fatal. A file without a
    '---' front matter renders as a bare fragment of elements, so it is wrapped in a
    root that declares kramdown-rfc's entities. Control characters XML forbids (a form
    feed in the Markdown, say) pass through kramdown-rfc unchanged, so they are read as
    spaces; xml2rfc would refuse such a draft, so mpp-specs' own build catches it.
    """
    document = XML_INVALID_RE.sub(" ", document)
    if not document.lstrip().startswith("<?xml"):
        document = FRAGMENT_PROLOG + "<rfc>" + document + "</rfc>"
    builder = ET.TreeBuilder(insert_pis=True)
    parser = xml.parsers.expat.ParserCreate()
    if comments is not None:
        parser.CommentHandler = comments.append
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.ExternalEntityRefHandler = lambda *_: 1
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.ProcessingInstructionHandler = builder.pi
    try:
        parser.Parse(document.encode("utf-8"), True)
    except xml.parsers.expat.ExpatError as error:
        raise ValueError(f"cannot parse kramdown-rfc output: {error}") from None
    return builder.close()


def is_element(node: ET.Element) -> bool:
    """Whether node is an element rather than a processing instruction or comment."""
    return isinstance(node.tag, str)


def inline_items(element: ET.Element) -> list[tuple[str, str] | ET.Element]:
    """Return an element's content in order: paragraphs of its own text and block children.

    Each paragraph is (text, prose). text is what a reader sees, for the excerpt and the
    hash: code spans in backticks, a reference without text as [target]. prose is the
    part a rule is read from: no code spans, autolinks or reference targets.

    A block child (BLOCK_TAGS) is returned as the element, to be read on its own in its
    place, and splits the text around it: in kramdown-rfc's default vocabulary a list
    item's paragraph after a nested list is the <list>'s tail inside the item's <t>, and
    a paragraph after a blank line in an item follows a <vspace blankLines="1"/>, yet
    each renders as a paragraph of its own. A plain line break (<br/>, <vspace/>) only
    adds a space.
    """
    items: list[tuple[str, str] | ET.Element] = []
    text: list[str] = []
    prose: list[str] = []

    def end_paragraph() -> None:
        items.append(("".join(text), "".join(prose)))
        text.clear()
        prose.clear()

    def both(value: str | None) -> None:
        if value:
            text.append(value)
            prose.append(value)

    def walk(node: ET.Element) -> None:
        both(node.text)
        for child in node:
            if not is_element(child):
                pass  # a processing instruction: only its tail is text
            elif child.tag in BLOCK_TAGS:
                end_paragraph()
                items.append(child)
            elif child.tag == "vspace" and child.get("blankLines", "0") != "0":
                end_paragraph()
            elif child.tag in ("vspace", "br"):
                both(" ")
            elif child.tag in HIDDEN_INLINE_TAGS:
                pass
            elif child.tag == "tt" or (
                child.tag == "spanx" and child.get("style") == "verb"
            ):
                # A code span: verbatim, so a keyword in it is no rule.
                text.append("\x60" + "".join(child.itertext()) + "\x60")
                prose.append(" ")
            elif child.tag == "xref":
                label = "".join(child.itertext())
                text.append(label or f"[{child.get('target', '')}]")
                prose.append(label or " ")
            elif child.tag == "eref":
                label = "".join(child.itertext())
                target = child.get("target", "")
                if not label or label == target:
                    # An autolink: the URL is an address, not prose ('<https://x/MUST>').
                    text.append(label or target)
                    prose.append(" ")
                else:
                    walk(child)  # link text is prose; the target is an attribute
            else:
                walk(child)  # emphasis, <bcp14>, sub/superscript and the like
            both(child.tail)

    walk(element)
    end_paragraph()
    return items


def inline_text(element: ET.Element) -> tuple[str, str]:
    """Return all of an element's own inline text as one (text, prose) pair."""
    segments = [item for item in inline_items(element) if isinstance(item, tuple)]
    return (
        " ".join(text for text, _ in segments),
        " ".join(prose for _, prose in segments),
    )


class SourceLines:
    """Finds the Markdown line a rendered rule starts on, by matching its words.

    kramdown-rfc's output carries <?line?> instructions only at section boundaries, so a
    rule is placed by finding LOCATE_WINDOW consecutive words of its text among the
    Markdown's words, searching from the previous rule onwards (rules come in document
    order). A window that contains words the Markdown spells differently (a character
    reference, say) fails, so later windows of the rule are tried too.
    """

    def __init__(self, markdown: str) -> None:
        # kramdown ends lines at LF, CR and CRLF only; str.splitlines would also split at
        # U+2028 and the like, which kramdown keeps inside a line.
        self.lines = re.sub(r"\r\n?", "\n", markdown).split("\n")
        self.words: list[str] = []
        self.word_lines: list[int] = []
        for number, line in enumerate(self.lines, start=1):
            for word in WORD_RE.findall(line):
                self.words.append(word)
                self.word_lines.append(number)
        self.positions: dict[str, list[int]] = {}
        for index, word in enumerate(self.words):
            self.positions.setdefault(word, []).append(index)
        self.cursor = 0

    def find(self, words: list[str], start: int) -> int | None:
        """Return the index in self.words where words begin, searching from start.

        Long windows first, so rules that open with the same phrase are told apart;
        shorter ones when the rendering spells too many words differently.
        """
        # Every candidate costs a slice comparison, and a common first word can have
        # thousands of them. The budget keeps a rule the matcher cannot place (rendered
        # words the Markdown spells differently) from making a draft quadratic: when it
        # runs out, the caller falls back to the <?line?> number.
        budget = LOCATE_BUDGET
        for size in dict.fromkeys(
            min(n, len(words)) for n in (LOCATE_WINDOW, LOCATE_WINDOW // 2, 3)
        ):
            for offset in range(len(words) - size + 1):
                window = words[offset : offset + size]
                candidates = self.positions.get(window[0], [])
                for index in candidates[bisect_left(candidates, start + offset) :]:
                    budget -= 1
                    if budget < 0:
                        return None
                    if self.words[index : index + size] == window:
                        return index - offset
        return None

    def locate(self, text: str, fallback: int) -> int:
        """Return the line a rule with this text starts on, or fallback when not found."""
        words = WORD_RE.findall(text)
        if not words:
            return fallback
        found = self.find(words, self.cursor)
        if found is None:
            found = self.find(words, 0)
        if found is None:
            return fallback
        # Rules do not overlap, so the next one starts after this one's words. Only half of
        # them are skipped: the Markdown can hold fewer words than the rendering (a
        # character reference such as '&#77;UST' renders as one word, MUST).
        self.cursor = found + max(1, len(words) // 2)
        return self.word_lines[found]

    def anchor_line(self, anchor: str, fallback: int) -> int:
        """Return the first line naming an anchor (the IAL that set it), or fallback."""
        for number, line in enumerate(self.lines, start=1):
            if anchor in line:
                return number
        return fallback


def requirements_from_xml(
    document: str, markdown: str, relative: str, draft: str
) -> tuple[list[Requirement], list[str], int]:
    """Extract every rule from one draft's kramdown-rfc XML.

    Returns the rules, warnings and the number of table rows holding a keyword. Read:
    the abstract, notes, the middle and appendices; never references, headings, code or
    figures. A rule is a paragraph of a <t>, list item or definition list entry with a
    keyword in its prose. Its ID is the element's anchor; a list item whose text
    kramdown-rfc wrapped in a paragraph passes its anchor to that paragraph. Table rows
    cannot carry an ID each, so they are counted rather than extracted; schemas cover
    them. A requirement anchor anywhere else (a heading, a list, a table, a span of
    text) tags nothing, and the report says so.
    """
    source = SourceLines(markdown)
    requirements: list[Requirement] = []
    warnings: list[str] = []
    table_rows = 0
    # The line of the latest <?line N?> instruction: the fallback for a rule the word
    # matcher cannot place. kramdown-rfc writes -N after an expanded boilerplate.
    pi_line = 0
    # Elements whose anchor has been used as a rule's ID or warned about; every other
    # requirement anchor is reported at the end, wherever kramdown-rfc put it.
    handled: set[int] = set()

    def misplaced(owner: ET.Element | None, place: str) -> None:
        """Warn when owner carries a requirement anchor, which tags nothing on place."""
        if owner is None:
            return
        handled.add(id(owner))
        anchor = owner.get("anchor") or ""
        if anchor.startswith("req-"):
            line = source.anchor_line(anchor, pi_line)
            warnings.append(
                f"{relative}:{line}: {anchor} goes on {place}, so it tags nothing"
            )

    def count_table(table: ET.Element) -> int:
        """Count a table's rows with a keyword in a cell's prose, header rows included."""
        if table.tag == "texttable":
            # kramdown-rfc's default vocabulary: <ttcol> headings, then the cells (<c>)
            # of every row in order, as many per row as there are columns.
            headings = [inline_text(cell)[1] for cell in table.iter("ttcol")]
            cells = [inline_text(cell)[1] for cell in table.iter("c")]
            width = max(1, len(headings))
            rows = [headings] + [
                cells[i : i + width] for i in range(0, len(cells), width)
            ]
        else:
            rows = [
                [" ".join(inline_prose(cell)) for cell in row if is_element(cell)]
                for row in table.iter("tr")
            ]
        return sum(1 for row in rows if keywords_in(" ".join(row)))

    def add_paragraph(text: str, prose: str, section: str, anchor: str | None) -> None:
        line = source.locate(text, pi_line)
        for ial in LITERAL_IAL_RE.finditer(prose):
            literal = LITERAL_IAL_ID_RE.search(ial.group())
            if literal:
                warnings.append(
                    f"{relative}:{line}: {literal.group(1)} ends a line of text, where kramdown-rfc "
                    "renders it as text, so it tags nothing; put it on the next line"
                )
        req_id = anchor if anchor and anchor.startswith("req-") else None
        found = keywords_in(prose)
        if not found:
            if req_id:
                warnings.append(
                    f"{relative}:{line}: {req_id} tags text without a BCP 14 keyword"
                )
            return
        normalized = normalize(text)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        malformed = req_id is not None and not REQ_ID_RE.match(req_id)
        if malformed:
            warnings.append(
                f"{relative}:{line}: {req_id} does not match req-<draft>-<nnn>; not counted as tagged or covered"
            )
        requirements.append(
            Requirement(
                draft=draft,
                file=relative,
                line=line,
                section=section,
                text=normalized,
                req_id=req_id,
                fallback_key=f"{draft}#{section}:{digest}",
                keywords=found,
                # The schemas only accept well-formed IDs in claims, so no claim can
                # cite this one: it is reported under its ID but not counted as tagged.
                id_problem="malformed" if malformed else None,
            )
        )

    def read_rule_block(
        element: ET.Element, section: str, owner: ET.Element | None
    ) -> None:
        """Read a paragraph, list item or definition entry and the blocks inside it, in order.

        Each paragraph of its own text with a keyword is a rule. owner is the element
        whose anchor is the rule's ID (element itself, or the list item that passed its
        anchor down); the anchor goes to the first paragraph, the one an IAL tags. A list
        item kramdown-rfc wraps in paragraphs (<li><t>...</t></li>) has no text of its
        own, so its anchor goes to the paragraph that starts it.
        """
        consumed = owner is None
        for item in inline_items(element):
            if isinstance(item, tuple):
                text, prose = item
                if not normalize(text):
                    continue
                anchor = None
                if not consumed:
                    handled.add(id(owner))
                    anchor = owner.get("anchor")  # type: ignore[union-attr]
                    consumed = True
                add_paragraph(text, prose, section, anchor)
            else:
                passes = (
                    not consumed and element.tag in ("li", "dd") and item.tag == "t"
                )
                visit(item, section, owner if passes else None)
                consumed = consumed or passes
        if not consumed:
            # No text of its own: kramdown-rfc's paragraph around a <list>, a thematic
            # break, or an item whose first block is no paragraph.
            if element.find("list") is not None:
                place = "a list"
            elif element.tag in ("li", "dd"):
                place = "a list item without text"
            else:
                place = "a block without text"
            misplaced(owner, place)

    def visit(
        child: ET.Element, section: str, inherited: ET.Element | None = None
    ) -> None:
        """Read one block; inherited is a list item whose anchor this block takes."""
        nonlocal table_rows
        tag = child.tag
        if (
            tag in ("references", "reference", "name", "title")
            or tag in HIDDEN_INLINE_TAGS
        ):
            return
        if tag == "front":
            for part in child:
                if is_element(part) and part.tag in FRONT_RULE_TAGS:
                    walk(part, part_label(part))
        elif tag in FRONT_RULE_TAGS:
            walk(child, part_label(child))
        elif tag == "section":
            misplaced(child, ANCHOR_PLACES["section"])
            walk(child, child.get("anchor") or slugify(section_title(child)))
        elif tag in CODE_TAGS or tag in TABLE_TAGS:
            misplaced(child, ANCHOR_PLACES[tag])
            if tag in TABLE_TAGS:
                table_rows += count_table(child)
        elif tag in RULE_TAGS:
            if child.get("anchor") is not None:
                if inherited is not None:
                    misplaced(inherited, "a list item without text")
                inherited = child
            elif inherited is not None:
                handled.add(id(child))
            read_rule_block(child, section, inherited)
        elif tag in ("middle", "back"):
            # Text outside any section, such as back matter before the first appendix,
            # is labelled with the part it is in.
            walk(child, tag)
        else:
            misplaced(child, ANCHOR_PLACES.get(tag, f"a <{tag}> element"))
            walk(child, section)

    def walk(parent: ET.Element, section: str) -> None:
        """Read parent's children in order, following its <?line N?> instructions."""
        nonlocal pi_line
        for child in parent:
            if is_element(child):
                visit(child, section)
            elif child.tag is ET.ProcessingInstruction and child.text:
                match = re.fullmatch(r"line\s+(-?\d+)\s*", child.text)
                if match:
                    pi_line = abs(int(match.group(1)))

    comments: list[str] = []
    root = parse_xml(document, comments)
    walk(root, "abstract")
    # Requirement anchors the walk never reached as a rule or a block: on a span of text
    # ('*MUST*{: #req-...}' anchors the <em>), a table cell, a reference and the like.
    for element in root.iter():
        anchor = element.get("anchor") if is_element(element) else None
        if anchor and anchor.startswith("req-") and id(element) not in handled:
            place = ANCHOR_PLACES.get(element.tag) or (
                "a span of text"
                if element.tag in SPAN_TAGS
                else f"a <{element.tag}> element"
            )
            misplaced(element, place)
    # Requirement IDs the Markdown sets that the rendering never shows: an IAL another
    # one replaced, one held for a block that never came, a definition no IAL names.
    # An ID that appears as text or in another attribute (in code, a comment, a literal
    # IAL, title="#req-...") is skipped: it is either no IAL or already reported.
    rendered = {e.get("anchor") for e in root.iter() if is_element(e)}
    shown = " ".join(
        [*root.itertext(), *comments]
        + [value for e in root.iter() if is_element(e) for value in e.attrib.values()]
    )
    for req_id in dict.fromkeys(SOURCE_ID_RE.findall(markdown)):
        if req_id not in rendered and req_id not in shown:
            line = source.anchor_line(req_id, 0)
            warnings.append(
                f"{relative}:{line}: {req_id} is on no element kramdown-rfc renders, so it tags "
                "nothing (another IAL replaced it, or no block follows it)"
            )

    seen: dict[str, int] = {}
    for requirement in requirements:
        # Two rules with the same text in one section would share a fallback key.
        count = seen.get(requirement.fallback_key, 0) + 1
        seen[requirement.fallback_key] = count
        if count > 1:
            requirement.fallback_key = f"{requirement.fallback_key}-{count}"
        # Exactly req-{draft}-NNN: a bare prefix check would take req-{draft}-charge-001,
        # an ID of draft {draft}-charge, as this draft's own. A malformed ID already has
        # its own warning; calling it foreign too would point at the wrong repair.
        if (
            requirement.req_id
            and requirement.id_problem != "malformed"
            and not re.fullmatch(
                rf"req-{re.escape(draft)}-[0-9]{{3}}", requirement.req_id
            )
        ):
            requirement.id_problem = requirement.id_problem or "foreign"
            warnings.append(
                f"{relative}:{requirement.line}: {requirement.req_id} does not use this draft's prefix req-{draft}-; not counted as covered"
            )
    return requirements, warnings, table_rows


def part_label(part: ET.Element) -> str:
    """Return the section label of the abstract or a note."""
    if part.tag == "abstract":
        return "abstract"
    return part.get("anchor") or slugify(part.get("title") or section_title(part))


def inline_prose(cell: ET.Element) -> list[str]:
    """Return a v3 table cell's prose, its inline text and that of the blocks in it."""
    parts = [inline_text(cell)[1]]
    for block in cell.iter():
        if block is not cell and is_element(block) and block.tag in RULE_TAGS:
            parts.append(inline_text(block)[1])
    return parts


def section_title(element: ET.Element) -> str:
    """Return the text of an element's <name> heading, or ''."""
    name = element.find("name")
    return "".join(name.itertext()) if name is not None else ""


def extract_requirements(
    path: Path, relative: str, render: Renderer
) -> tuple[list[Requirement], list[str], int]:
    """Render one draft with kramdown-rfc and extract its rules (requirements_from_xml)."""
    data = path.read_bytes()
    document = render(data)
    markdown = data.decode("utf-8", "replace")
    return requirements_from_xml(
        document, markdown, relative, draft_short_name(path.name)
    )


def draft_paths(specs_dir: Path) -> list[Path]:
    """Return the drafts under specs/, or fail when specs_dir is not an mpp-specs checkout."""
    if not specs_dir.is_dir():
        raise ValueError(f"specs path {specs_dir} does not exist or is not a directory")
    specs = specs_dir / "specs"
    # A symlinked specs/ (or draft) could point anywhere; its text would be copied into the
    # report. Refusing the link itself keeps root inside the checkout, so the containment
    # check below also catches symlinked directories under specs/ that lead out of it.
    if specs.is_symlink():
        raise ValueError(f"{specs} is a symlink; pass an mpp-specs checkout")
    root = specs.resolve()
    # A symlinked draft is refused rather than skipped: skipping it would drop its rules
    # from a report that still names a clean commit.
    links = sorted(p for p in specs.rglob("*.md") if p.is_symlink())
    if links:
        raise ValueError(f"{links[0]} is a symlink; drafts must be regular files")
    paths = sorted(
        p
        for p in specs.rglob("*.md")
        # is_file: rglob also matches a directory named *.md, which read_text cannot read.
        if p.is_file() and p.resolve().is_relative_to(root)
    )
    # Every specs/**/*.md is read as a draft, as mpp-specs' gen.sh and linter do, but a
    # tree with no draft-*.md at all (only a README, say) is not an mpp-specs checkout.
    if not any(p.name.startswith("draft-") for p in paths):
        raise ValueError(
            f"{specs_dir} has no specs/**/draft-*.md drafts; pass an mpp-specs checkout"
        )
    return paths


def extract_all(
    specs_dir: Path, render: Renderer
) -> tuple[list[Requirement], list[str], dict[str, int], dict[str, str]]:
    """Extract requirements from every draft under specs/, flagging duplicate IDs.

    Returns the requirements, the warnings, each draft's keyword table rows and each
    draft's file. The last two list every draft read, including one with no prose rules
    (such as a draft whose keywords are all in tables), so the report still shows it.
    """
    warnings: list[str] = []
    first_path: dict[str, str] = {}
    selected: list[tuple[Path, str]] = []

    # Highest revision first, so a stray older copy of a draft is the one skipped. Sort on
    # the -NN number, not the name: by name an unversioned draft-x.md ('.' > '-') would beat
    # draft-x-02.md. A file without a revision ranks below every explicit one.
    def revision(path: Path) -> tuple[int, str]:
        match = re.search(r"-(\d{2})\.md$", path.name)
        return (int(match.group(1)) if match else -1, path.name)

    for path in sorted(draft_paths(specs_dir), key=revision, reverse=True):
        relative = path.relative_to(specs_dir).as_posix()
        short = draft_short_name(path.name)
        if short in first_path:
            # Two files with one short name would merge their rules into one draft.
            warnings.append(
                f"{relative}: same draft name '{short}' as {first_path[short]}; skipped"
            )
            continue
        first_path[short] = relative
        selected.append((path, relative))

    def extract(item: tuple[Path, str]) -> tuple[list[Requirement], list[str], int]:
        path, relative = item
        try:
            return extract_requirements(path, relative, render)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise ValueError(f"{relative}: {error}") from None

    # Each render starts a Ruby process that takes about a second, mostly startup, so the
    # drafts are rendered in parallel; map keeps the results in draft order.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, os.cpu_count() or 1)
    ) as pool:
        results = list(pool.map(extract, selected))
    requirements: list[Requirement] = []
    table_rows: dict[str, int] = {}
    for (path, _), (found, found_warnings, rows) in zip(selected, results):
        requirements.extend(found)
        warnings.extend(found_warnings)
        table_rows[draft_short_name(path.name)] = rows
    # Coverage is matched by ID, so one claim on an ID that several rules carry would
    # cover all of them. Every rule sharing an ID is marked, the first one included,
    # since nothing says which of them the claim's author meant.
    sharing: dict[str, list[Requirement]] = {}
    for requirement in requirements:
        if requirement.req_id:
            sharing.setdefault(requirement.req_id, []).append(requirement)
    for req_id, users in sharing.items():
        if len(users) < 2:
            continue
        first = users[0]
        for other in users[1:]:
            warnings.append(
                f"{other.file}:{other.line}: {req_id} already used at {first.file}:{first.line}; neither is counted as covered"
            )
        for requirement in users:
            requirement.id_problem = requirement.id_problem or "duplicate"
    return requirements, warnings, table_rows, first_path


@dataclass
class Claim:
    """A vector scenario or flow case and the requirement IDs it covers."""

    source: str
    requirements: list[str]
    operation: str | None = None
    adapters: list[str] | None = None
    sdk_versions: dict[str, str] | None = None
    # Flow cases only: server_only, unsent, raw_http, client_flow or ordinary, as flow_runner runs them.
    flow_mode: str | None = None
    # Why no SDK can pass this claim for real, if none can: the vector runner refuses the
    # claim's whole file, or the scenario calls a legacy command that maps to no adapter
    # operation (the runner fails it before starting any SDK, so it could only "pass" by
    # expecting that failure); or a flow case's challenge_path names no case's path.
    runner_error: str | None = None
    # Further operations the claim calls, each of which the adapter must advertise: for a
    # vector claim the ones its scenario's tests call besides operation (a parse and format
    # scenario needs both); for an ordinary flow case all of them (flow_operations).
    operations: tuple[str, ...] = ()
    # Vector claims only: per adapter, the sdkVersions expressions of every scenario in this
    # claim's file that selects the adapter (this scenario included), in file order. As the
    # runner walks the file for an adapter it resolves the adapter's SDK version and parses
    # each such expression; if either raises, the whole file is aborted for that adapter.
    file_constraints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Vector claims only: adapters for which the runner crashes on this claim's file (an
    # adapter-restricted generate scenario with no expected value), so it covers nothing
    # for them while the other adapters run the file normally.
    crashed_adapters: tuple[str, ...] = ()


def scenario_commands(vector: dict[str, Any], scenario: dict[str, Any]) -> tuple[str, ...]:
    """Return the legacy commands a scenario's tests call, as _run_scenario dispatches them.

    commands.operation calls no legacy command; a generate command is the only one called
    when present. A test whose input is null (no wire value to parse, no object to
    format) is skipped by the runner and calls nothing. Otherwise a parse test calls the parse command, a format test the format
    command, and a roundtrip (which runs only when both exist) calls both. A format test
    expecting success (true, not "exact") compares the formatted value semantically, by
    parsing both wire values with the parse command (except for base64url-encode), so it
    calls the parse command as well.
    """
    commands = vector["commands"]
    if "operation" in commands:
        return ()
    if "generate" in commands:
        return (commands["generate"],)
    tests = scenario.get("tests") or {}
    parse, format_ = commands.get("parse"), commands.get("format")
    # The runner reads its inputs as _run_scenario does: a base64url vector's decoded and
    # encoded values, otherwise object and wire. It skips a parse test with no wire value
    # and a format or roundtrip test with no object, so such a test checks nothing.
    if parse and parse.startswith("base64url-"):
        obj, wire = scenario.get("decoded"), scenario.get("encoded")
    else:
        obj, wire = scenario.get("object"), scenario.get("wire")
    runs_parse = tests.get("parse") is not None and bool(parse) and wire is not None
    runs_format = tests.get("format") is not None and bool(format_) and obj is not None
    roundtrip = tests.get("roundtrip") is True and bool(parse) and bool(format_) and obj is not None
    semantic_format = runs_format and tests.get("format") is True and bool(parse) and format_ != "base64url-encode"
    needed = []
    if parse and (runs_parse or roundtrip or semantic_format):
        needed.append(parse)
    if format_ and (runs_format or roundtrip):
        needed.append(format_)
    return tuple(needed)


def scenario_operations(vector: dict[str, Any], scenario: dict[str, Any]) -> tuple[str, ...]:
    """Return the adapter operations a scenario's tests call; the adapter needs each one.

    An explicit commands.operation is the only one. Legacy commands are mapped through
    COMMAND_TO_OPERATION; an unmapped one is left out here and caught by load_claims.
    """
    commands = vector["commands"]
    if "operation" in commands:
        return (commands["operation"],)
    return tuple(
        COMMAND_TO_OPERATION[command]
        for command in scenario_commands(vector, scenario)
        if command in COMMAND_TO_OPERATION
    )


def check_scenario(runner: VectorRunner, scenario: dict[str, Any]) -> None:
    """Raise ValueError if the vector runner refuses this scenario for every adapter.

    That is its structure: sdkVersions keys and value types, and adapters names, which
    the runner checks for every scenario in a file before running any of them. A
    malformed constraint expression only aborts the file for the adapter it is keyed by,
    when the runner reaches it for that adapter; version_checker models that.
    """
    runner.scenario_version_constraints(scenario)
    runner.scenario_adapters(scenario)


def operation_input_error(
    vector: dict[str, Any], scenario: dict[str, Any], vector_name: str
) -> str | None:
    """Why the runner fails an operation scenario for every SDK before calling one, if it does.

    For an operation vector whose scenario does not expect ok: false, vector_runner sends
    the request with validate_input on, and AdapterClient.call validates it against
    adapter-request.schema.json before starting the adapter. An input the schema rejects
    raises there, the runner records a failure, and no SDK process runs, so the claim checks
    nothing. A negative scenario skips that validation and reaches the SDK.

    Legacy parse, format and generate tests go through run_legacy_command, which builds the
    request with request_input_for_command and validates it the same way unless that test
    expects success: false. A request it cannot build or the schema rejects becomes an error
    result, so that test fails for every SDK, and the claim counts for nothing. Only the
    first call of each test is checked here; the semantic and roundtrip follow-up parses take
    adapter output, which is not known in advance.
    """
    commands = vector.get("commands", {})
    operation = commands.get("operation")
    if not operation:
        return legacy_input_error(commands, scenario)
    if expects_failure(scenario.get("expected"), "ok"):
        return None
    request = {
        "schema": 1,
        "op": operation,
        "input": scenario.get("input"),
        "context": {"caseName": scenario["name"], "vectorName": vector_name},
    }
    try:
        validate_value(request, "adapter-request.schema.json", "request")
    except ValueError as error:
        return f"its input fails the adapter request schema, so the runner fails it before any SDK runs ({error})"
    return None


def legacy_input_error(commands: dict[str, Any], scenario: dict[str, Any]) -> str | None:
    """Why run_legacy_command fails a legacy test's first call for every SDK, if it does.

    Mirrors vector_runner._run_scenario: a generate command alone when present, else a parse
    test on the wire value, a format test on the object and a roundtrip test's format call,
    each with the input and the expectation the runner gives it.
    """
    tests = scenario.get("tests") or {}
    parse, format_, generate = commands.get("parse"), commands.get("format"), commands.get("generate")
    calls: list[tuple[str, Any, Any]] = []
    if generate is not None:
        test = tests.get("generate")
        expected = test if test is not None and test is not True else {"success": True}
        calls.append((generate, json.dumps(scenario.get("input")), expected))
    else:
        base64url = bool(parse) and str(parse).startswith("base64url-")
        obj = scenario.get("decoded") if base64url else scenario.get("object")
        wire = scenario.get("encoded") if base64url else VectorRunner().scenario_wire(scenario)
        parse_test = tests.get("parse")
        if parse_test is not None and parse and wire is not None:
            calls.append((parse, wire, {"success": True} if parse_test is True else parse_test))
        format_test = tests.get("format")
        if format_test is not None and format_ and obj is not None:
            expected = {"success": True} if format_test in (True, "exact") else format_test
            calls.append((format_, obj if base64url else json.dumps(obj), expected))
        # A roundtrip test starts with a format call on the object, validated like any other.
        if tests.get("roundtrip") is True and parse and format_ and obj is not None:
            calls.append((format_, obj if base64url else json.dumps(obj), {"success": True}))
    for command, input_data, expected in calls:
        if expects_failure(expected):
            continue
        try:
            op, input_value = request_input_for_command(command, input_data)
            request = {"schema": 1, "op": op, "input": input_value}
            validate_value(request, "adapter-request.schema.json", "request")
        except Exception as error:  # noqa: BLE001 - run_adapter turns any of them into a failure
            return (
                f"its {command} request fails the adapter request schema, so the runner "
                f"fails that test before any SDK runs ({error})"
            )
    return None


def load_claims(
    vectors_dir: Path = VECTORS_DIR, flow_cases_path: Path = FLOW_CASES_PATH
) -> list[Claim]:
    """Read the requirement IDs each vector scenario and flow case claims."""
    claims: list[Claim] = []
    runner = VectorRunner()
    for path in sorted(vectors_dir.glob("*.json")):
        if path.name == "package.json":
            continue
        vector = load_vector(path)
        # The vector runner validates every scenario's sdkVersions and adapters as it loops
        # over a file, and one invalid scenario aborts the whole file for every adapter. So
        # one bad scenario, tagged or not, makes every claim in the file coverage for nothing.
        file_error = None
        # Adapters for which the runner crashes on this file, when only some of them do.
        crashed: set[str] = set()
        commands = vector.get("commands", {})
        for scenario in vector["scenarios"]:
            try:
                check_scenario(runner, scenario)
            except ValueError as error:
                file_error = f"the runner refuses its file (vectors/{path.name}#{scenario['name']}: {error})"
                break
            # A generate scenario whose tests.generate is missing or true is compared with
            # scenario["expected"]; without one the runner raises KeyError, which aborts
            # the whole file (_run_scenarios lets it escape), so nothing in it runs. The
            # runner drops a scenario whose adapters list leaves the adapter out before it
            # runs any, so such a scenario crashes the file only for the adapters it lists
            # (all of them when it lists none). A version skip would also spare an adapter;
            # it is not modelled, so this errs toward less coverage, never more.
            # compare_adapter_response reads an operation scenario's expected as an
            # object, and its error as one when ok is false; the schema allows any value,
            # and anything else raises AttributeError, which also aborts the file. A
            # messageContains that isn't a string raises TypeError in its "in" test once the
            # adapter returns the expected error type, which is the case the vector is for.
            # Treated like the generate crash below: for the listed adapters, or for all.
            expected = scenario.get("expected")
            error = expected.get("error") if isinstance(expected, dict) else None
            if commands.get("operation") and (
                not isinstance(expected, dict)
                or (
                    expected.get("ok") is False
                    and "error" in expected
                    and (
                        not isinstance(error, dict)
                        or (
                            error.get("messageContains") is not None
                            and not isinstance(error["messageContains"], str)
                        )
                    )
                )
            ):
                selected = scenario.get("adapters")
                if selected:
                    crashed.update(selected)
                    continue
                file_error = (
                    f"the runner crashes on its file (vectors/{path.name}#{scenario['name']} "
                    "has an expected value or error it cannot compare)"
                )
                break
            generate = scenario.get("tests", {}).get("generate")
            if (
                commands.get("generate")
                and not commands.get("operation")
                and "expected" not in scenario
                and generate in (None, True)
            ):
                selected = scenario.get("adapters")
                if selected:
                    crashed.update(selected)
                    continue
                file_error = (
                    f"the runner crashes on its file (vectors/{path.name}#{scenario['name']} "
                    "has no expected value for its generate test)"
                )
                break
        file_constraints: dict[str, tuple[str, ...]] = {}
        if not file_error:
            for scenario in vector["scenarios"]:
                selected = scenario.get("adapters")
                for adapter, expression in (scenario.get("sdkVersions") or {}).items():
                    # The runner skips a scenario whose adapters list leaves the adapter
                    # out before it looks at the scenario's versions (an empty list, which
                    # the schema forbids anyway, selects every adapter, as in the runner).
                    if not selected or adapter in selected:
                        file_constraints[adapter] = file_constraints.get(adapter, ()) + (
                            expression,
                        )
        for scenario in vector["scenarios"]:
            if not scenario.get("requirements"):
                continue
            source = f"vectors/{path.name}#{scenario['name']}"
            operations = scenario_operations(vector, scenario)
            unmapped = [
                command
                for command in scenario_commands(vector, scenario)
                if command not in COMMAND_TO_OPERATION
            ]
            error = file_error
            if not error and unmapped:
                error = (
                    f"command '{unmapped[0]}' maps to no adapter operation, "
                    "so the runner fails it before any SDK runs"
                )
            elif not error and not operations:
                error = "its tests call none of the vector's commands"
            elif not error:
                error = operation_input_error(vector, scenario, path.stem)
            claims.append(
                Claim(
                    source=source,
                    requirements=scenario["requirements"],
                    operation=operations[0] if operations else None,
                    operations=operations[1:],
                    adapters=scenario.get("adapters"),
                    sdk_versions=scenario.get("sdkVersions"),
                    runner_error=error,
                    file_constraints=file_constraints,
                    crashed_adapters=tuple(sorted(crashed)),
                )
            )
    flow_cases = load_flow_cases(flow_cases_path)
    # flow_runner looks a challenge_path up among every case's path, server_only ones
    # included, and fails the case before sending anything when none has it.
    # Last case wins for a shared path, as in flow_runner's cases_by_path.
    cases_by_path = {str(flow_case.get("path", "/")): flow_case for flow_case in flow_cases}
    flow_paths = set(cases_by_path)
    # flow_runner fails a case with no entry in golden-results.json for every adapter
    # ("missing golden case"). Without the file (a test fixture) nothing is checked.
    golden_path = flow_cases_path.parent / "golden-results.json"
    golden = (
        {str(entry.get("name")): entry for entry in load_flow_results(golden_path)}
        if golden_path.exists()
        else None
    )
    names = [str(flow_case.get("name")) for flow_case in flow_cases]
    for flow_case in flow_cases:
        if flow_case.get("requirements"):
            challenge_path = flow_case.get("challenge_path")
            flow_error = None
            if challenge_path and str(challenge_path) not in flow_paths:
                flow_error = (
                    f"challenge_path {challenge_path} names no flow case's path, "
                    "so flow_runner fails it before any request"
                )
            elif names.count(str(flow_case["name"])) > 1:
                flow_error = (
                    "another flow case has its name, and flow_runner checks every case "
                    "against the one golden result recorded under that name"
                )
            elif golden is not None and str(flow_case["name"]) not in golden:
                flow_error = (
                    "it has no golden result, so flow_runner fails it for every adapter"
                )
            claims.append(
                Claim(
                    source=f"flows/{flow_cases_path.name}#{flow_case['name']}",
                    requirements=flow_case["requirements"],
                    flow_mode=flow_mode(flow_case, cases_by_path),
                    runner_error=flow_error,
                    operations=flow_operations(
                        flow_case,
                        (golden or {}).get(str(flow_case["name"])),
                        first_challenge_case(flow_case, cases_by_path),
                    ),
                )
            )
    return claims


def flow_operations(
    flow_case: dict[str, Any],
    golden_entry: dict[str, Any] | None,
    challenge_case: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """The adapter operations an ordinary flow case calls, as flow_runner runs it.

    flow_runner calls challenge.parse on the 402's WWW-Authenticate header. The
    compliance server sends a malformed header only when the case that answers the
    first request has invalid_www_authenticate; that is challenge_case (the
    challenge_path case, or the case a redirect lands on) when given, else flow_case.
    The parse then fails and the case ends there, so a parse-only adapter takes part
    in it. A case that sets the flag but takes its challenge from a valid case goes on
    like any other. Every other case goes on to credential.format,
    and a case whose golden result holds a Receipt also calls receipt.parse on the
    retry's Payment-Receipt header. AdapterClient.call answers an operation the adapter
    does not declare locally (unsupported_operation), so without one of these the SDK
    does not run that step and the case covers nothing for it. Without a golden entry
    (a test fixture without golden-results.json) no Receipt step is assumed.
    """
    # The flag of the case the server answered with decides, not the claiming case's.
    answered = challenge_case if challenge_case is not None else flow_case
    if answered.get("invalid_www_authenticate"):
        return ("challenge.parse",)
    operations: tuple[str, ...] = ("challenge.parse", "credential.format")
    if golden_entry is not None and golden_entry.get("receipt") is not None:
        operations += ("receipt.parse",)
    return operations


# Flow case flags under which the compliance server answers without a Payment challenge
# (flows/compliance-server.ts handles each before it builds one).
NO_CHALLENGE_FLAGS = ("force_status", "no_payment", "discovery", "json_rpc")


def flow_mode(
    flow_case: dict[str, Any], cases_by_path: dict[str, dict[str, Any]] | None = None
) -> str:
    """Classify a flow case the way flow_runner dispatches it.

    cases_by_path maps each flow case's path to its case, so a challenge_path resolves
    to the case whose response the runner actually reads first.
    """
    if flow_case.get("server_only"):
        return "server_only"
    # Same order as flow_runner's dispatch: discovery and JSON-RPC cases send raw
    # HTTP without the adapter; client_flow wins over no_payment; a remaining
    # no_payment case also never calls the adapter.
    # A JSON-RPC case posts a fixed JSON payload (perform_json_request), always sendable.
    if flow_case.get("json_rpc"):
        return "raw_http"
    # A discovery case sends its own request through perform_request.
    if flow_case.get("discovery"):
        return "raw_http" if request_sendable(flow_case) else "unsent"
    if flow_case.get("client_flow"):
        return "client_flow"
    # Every other case first sends the challenge_path case's request (its own when none
    # is named). When perform_request cannot send it, the case raises before the server
    # sees anything, so it checks nothing: "unsent", never coverage.
    first = flow_case
    if flow_case.get("challenge_path") and cases_by_path is not None:
        first = cases_by_path.get(str(flow_case["challenge_path"])) or flow_case
    if not request_sendable(first):
        return "unsent"
    if flow_case.get("no_payment"):
        return "raw_http"
    # The first request goes to challenge_path's case when one is named. The compliance
    # server answers that case without a challenge when it is a force_status (any status,
    # 402 included, with no WWW-Authenticate header), no_payment (200), discovery or
    # json_rpc (both 200) case, so flow_runner stops (unexpected_status or
    # missing_challenge) before it calls the adapter: it checks the reference server,
    # not an SDK.
    return "ordinary" if first_challenge_case(flow_case, cases_by_path) is not None else "raw_http"


def first_challenge_case(
    flow_case: dict[str, Any], cases_by_path: dict[str, dict[str, Any]] | None
) -> dict[str, Any] | None:
    """The case whose 402 challenge answers flow_runner's first request, or None.

    That is the challenge_path case when one is named, else flow_case, followed through
    any redirects to the case that finally answers (see answering_case). None means no
    challenge reaches the adapter.
    """
    first: dict[str, Any] | None = flow_case
    if flow_case.get("challenge_path") and cases_by_path is not None:
        # flow_runner fails with missing_challenge_case, before any adapter call.
        first = cases_by_path.get(str(flow_case["challenge_path"]))
    # flow_runner sends that first request with the method of the case it goes to
    # (perform_request(url, challenge_case)), the challenge_path case's when one is named.
    method = str((first or flow_case).get("http_method", "GET")).upper()
    if first is not None and not request_sendable(first):
        return None
    # flow_runner appends that case's initial_query to its path (flow_case_url).
    query = str((first or {}).get("initial_query") or "")
    return answering_case(first, cases_by_path, method=method, query=query)


def request_sendable(case: dict[str, Any]) -> bool:
    """Whether flow_runner's perform_request can send this case's request at all.

    perform_request encodes the case's body as UTF-8 for a POST, PUT or PATCH (its
    http_method as written) before it sends anything; a body that is not a string, or
    holds a lone surrogate, raises there. It also adds the case's Accept-Payment and
    Idempotency-Key headers (str() of each truthy value), which http.client refuses
    when they hold CR or LF or fall outside Latin-1, and refuses a URL with a control
    character, space or non-ASCII text. Either way no request reaches the server.
    """
    if case.get("http_method", "GET") in {"POST", "PUT", "PATCH"}:
        body = case.get("body")
        if body is not None:
            if not isinstance(body, str):
                return False
            try:
                body.encode("utf-8")
            except UnicodeEncodeError:
                return False
    for key in ("accept_payment", "idempotency_key"):
        value = case.get(key)
        if value and not header_value_sendable(str(value)):
            return False
    # flow_case_url appends initial_query to path; http.client refuses a request target
    # with a control character or space, and cannot encode one outside ASCII.
    query = case.get("initial_query")
    target = f"{case.get('path', '/')}{'' if query is None else query}"
    return all(0x20 < ord(char) < 0x7F for char in target)


def header_value_sendable(value: str) -> bool:
    """Whether http.client sends this header value: Latin-1 only, with no CR or LF.

    http.client accepts a CR or LF followed by a space or tab (obsolete line folding),
    but the compliance server's parser rejects those, so they count as unsendable too.
    """
    return "\r" not in value and "\n" not in value and all(ord(char) < 256 for char in value)


# Printable ASCII in the WHATWG path percent-encode set (URL Standard, section 1.3).
PATH_PERCENT_ENCODE = frozenset('"<>^{}' + "\x60")


def lookup_path(path: str, query: str = "") -> str | None:
    """The pathname the compliance server looks a request for path + query up by.

    None when no case can match it: the server keys cases by the WHATWG-normalized
    pathname, so dot segments, backslashes, characters the URL parser percent-encodes,
    and spaces, controls or non-ASCII text anywhere in path + query (which urllib
    refuses to send) never find the declared path.
    """
    # http.client refuses a request target with a space or control character (InvalidURL)
    # and cannot encode non-ASCII text, so such a path or initial_query is never sent.
    if any(ord(char) <= 0x20 or ord(char) >= 0x7F for char in path + query):
        return None
    try:
        pathname = urllib.parse.urlsplit(path + query).path
    except ValueError:
        # An invalid IPv6 netloc such as "//[" makes urlsplit raise; urllib cannot
        # send such a request either.
        return None
    if not pathname.startswith("/") or "\\" in pathname:
        return None
    # The WHATWG path percent-encode set: the server's URL parser encodes these, so the
    # pathname it looks up is no longer the declared one.
    if any(char in PATH_PERCENT_ENCODE for char in pathname):
        return None
    dots = {".", "..", "%2e", ".%2e", "%2e.", "%2e%2e"}
    if any(segment.lower() in dots for segment in pathname.split("/")):
        return None
    return pathname


# mppx's datetime() pattern (src/zod.ts), with ASCII digits as in JavaScript.
MPPX_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def charge_request_fits(request: Any) -> bool:
    """Whether the compliance server's mpp.charge accepts a flow case's request object.

    Mirrors the zod schema of baseMethod's request in compliance-server.ts: amount,
    currency, recipient and expires are required strings and resource an optional string.
    None means the case has no request, so the server uses its default, which fits.
    """
    if request is None:
        return True
    if not isinstance(request, dict):
        return False
    required = ("amount", "currency", "recipient", "expires")
    if any(not isinstance(request.get(key), str) for key in required):
        return False
    # mpp.charge lifts expires into the Challenge, whose schema (mppx zod.datetime)
    # rejects anything but this ISO 8601 form, so the server throws before any 402.
    if not MPPX_DATETIME.fullmatch(request["expires"]):
        return False
    # The server spreads the whole request into mpp.charge, which also reads description,
    # meta and scope as Challenge options (Challenge schema: description a string, meta a
    # record of strings, scope merged into meta) and throws on a mistyped one or on a scope
    # that conflicts with meta._mppx_scope. Only the plainly valid forms count.
    if "description" in request and not isinstance(request["description"], str):
        return False
    # Challenge.serialize throws on a CR or LF in a quoted-string value, so no 402 is sent.
    if re.search(r"[\r\n]", request.get("description") or ""):
        return False
    meta = request.get("meta")
    if "meta" in request and not (
        isinstance(meta, dict) and all(isinstance(value, str) for value in meta.values())
    ):
        return False
    if "scope" in request:
        scope = request["scope"]
        if not isinstance(scope, str):
            return False
        if isinstance(meta, dict) and "_mppx_scope" in meta and meta["_mppx_scope"] != scope:
            return False
    return "resource" not in request or isinstance(request["resource"], str)


def answers_challenge(
    flow_case: dict[str, Any] | None,
    cases_by_path: dict[str, dict[str, Any]] | None,
    hops: int = 0,
    method: str = "GET",
    query: str = "",
) -> bool:
    """Whether the compliance server answers flow_runner's first request with a 402 challenge.

    See answering_case, which also says which case's challenge it is.
    """
    return answering_case(flow_case, cases_by_path, hops, method, query) is not None


def answering_case(
    flow_case: dict[str, Any] | None,
    cases_by_path: dict[str, dict[str, Any]] | None,
    hops: int = 0,
    method: str = "GET",
    query: str = "",
) -> dict[str, Any] | None:
    """The case whose 402 challenge answers flow_runner's first request, or None if none does.

    Only then does flow_runner call the adapter. The server finds a case by the request's
    pathname alone (compliance-server.ts caseByPath), so a case whose path holds a query or
    fragment is never found (404). A case with a no-challenge flag answers without one. A
    redirect_to case answers 302, and flow_runner's urllib follows it: it reaches the adapter
    only when the target is another case on the same server that answers a challenge. A
    redirect_to_port_offset 1 lands on the server's second listener (port + 1), which serves
    the same handler and cases, so it counts like a same-server redirect; any other offset has
    no listener. A redirect to another host, to no case, a loop, or any redirect when
    cases_by_path is unknown does not count. urllib follows the server's 302
    only for GET, HEAD and POST (POST turns into GET), so a PUT, PATCH, DELETE or OPTIONS
    request stops at the 302 before any adapter call.

    Redirects are followed in a loop rather than by recursion, so a long redirect chain in a
    large flows.json cannot exceed Python's recursion limit.
    """
    while True:
        if flow_case is None or hops > len(cases_by_path or {}):
            return None
        path = str(flow_case.get("path", "/"))
        # The request must reach this case: its path plus any initial_query has to normalize
        # back to the path itself (a query or fragment in path, or an initial_query that adds
        # path segments, sends it to another pathname, usually a 404).
        if lookup_path(path, query) != path:
            return None
        # The server's order (compliance-server.ts): discovery, json_rpc and force_status answer
        # first, then redirect_to, and only then no_payment, so a redirect wins over no_payment.
        if any(flow_case.get(flag) for flag in ("discovery", "json_rpc", "force_status")):
            return None
        # compliance-server.ts redirects only when redirect_to is truthy, so an empty string (or
        # a port offset with no target) is no redirect and the case answers as if unset.
        target = flow_case.get("redirect_to")
        if not target:
            if flow_case.get("no_payment"):
                return None
            # A case with expect_problem_details answers the first request with that
            # problem's status; only a 402 carries the challenge flow_runner parses.
            problem = flow_case.get("expect_problem_details")
            if problem and problem.get("status") != 402:
                return None
            # mpp.charge validates the case's request (compliance-server.ts baseMethod: string
            # amount, currency, recipient and expires, optional string resource) and throws
            # before any 402 when it doesn't fit, so flow_runner never reaches the adapter.
            # A case without request gets the server's default one, which fits.
            # requestForFlowCase replaces resource with the request path when
            # bind_request_resource is set, so the case's own resource never reaches mppx.
            request = flow_case.get("request")
            if flow_case.get("bind_request_resource") and isinstance(request, dict):
                request = {**request, "resource": "/"}
            return flow_case if charge_request_fits(request) else None
        if method not in {"GET", "HEAD", "POST"}:
            return None
        try:
            split = urllib.parse.urlsplit(str(target))
        except ValueError:
            # A target urllib cannot parse (such as "http://[") is never followed.
            return None
        # compliance-server.ts listens on port and port + 1 with the same handler; only offset 1
        # (a JSON integer, not a bool) reaches a listener. Other offsets get connection refused.
        offset = flow_case.get("redirect_to_port_offset")
        if offset and not (type(offset) is int and offset == 1):
            return None
        if cases_by_path is None or split.scheme or split.netloc:
            return None
        # compliance-server.ts resolves the target with WHATWG new URL, which differs from
        # urljoin on some inputs: "///x" is another host, "\\" counts as "/", empty
        # segments ("a//b") are kept where urljoin drops them, percent-encoded dot segments
        # are removed, and tabs, newlines and edge spaces are stripped. Follow only targets
        # both resolve alike: printable ASCII without those forms; others count as no
        # challenge, which can only under-count.
        raw_target = str(target)
        if (
            raw_target.startswith("//")
            or "//" in split.path
            or any(not ("!" <= char <= "~") or char in "\\%" for char in raw_target)
        ):
            return None
        # urllib's default HTTPRedirectHandler follows at most max_redirections (10)
        # redirects and raises HTTPError on the next 302, which flow_runner reads as an
        # unexpected status before any adapter call.
        if hops >= urllib.request.HTTPRedirectHandler.max_redirections:
            return None
        target_path = urllib.parse.urljoin(path, split.path) if split.path else path
        next_method = "HEAD" if method == "HEAD" else "GET"
        # Follow the redirect: the target is requested with no initial_query. A loop stops
        # at the redirect cap above (or once hops exceeds the number of cases).
        flow_case, hops, method, query = cases_by_path.get(target_path), hops + 1, next_method, ""


def load_profiles(adapters_dir: Path = ADAPTERS_DIR) -> dict[str, dict[str, Any]]:
    """Read each adapter manifest's name, capabilities and optional profile."""
    profiles: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(adapters_dir.glob("*/adapter.json")):
        manifest = load_json(manifest_path)
        validate_value(manifest, "adapter-manifest.schema.json", str(manifest_path))
        if manifest.get("profile"):
            profiles[manifest["name"]] = {
                "profile": manifest["profile"],
                "capabilities": manifest["capabilities"],
            }
        else:
            # The last manifest with a name wins, as in harness's discover_adapters and
            # load_capabilities, so a later one without a profile drops an earlier one's.
            profiles.pop(manifest["name"], None)
    return profiles


def load_capabilities(adapters_dir: Path = ADAPTERS_DIR) -> dict[str, list[str]]:
    """Read every adapter manifest's name and capabilities, profile or not.

    These are the adapters the vector runner discovers and can run (harness's
    discover_adapters reads the same manifests), so they decide whether any SDK runs a
    vector claim at all.
    """
    capabilities: dict[str, list[str]] = {}
    for manifest_path in sorted(adapters_dir.glob("*/adapter.json")):
        manifest = load_json(manifest_path)
        validate_value(manifest, "adapter-manifest.schema.json", str(manifest_path))
        capabilities[manifest["name"]] = manifest["capabilities"]
    return capabilities


VersionCheck = Callable[[Claim, str], bool]


def claim_applies(
    claim: Claim,
    adapter: str,
    capabilities: list[str],
    version_applies: VersionCheck | None = None,
) -> bool:
    """Whether the runners would run this claim against the adapter.

    This is what profile coverage means: the adapter takes part in a case that claims
    the rule. It does not judge whether the rule is client- or server-side; an ordinary
    flow runs the adapter as the client against the fixed mppx server, so a server-side
    rule should be claimed by server-verification, not by flows. server_only cases are
    never run (flow_runner skips them), so they apply to no adapter.
    """
    if claim.flow_mode is not None:
        # server_only and raw_http cases never call the adapter; only client_flow
        # cases drive the adapter's http.payment_request.
        if claim.flow_mode in ("server_only", "unsent", "raw_http"):
            return False
        if claim.flow_mode == "client_flow":
            return "http.payment_request" in capabilities
        # An ordinary case drives the SDK through the operations flow_operations lists
        # (challenge.parse, then credential.format and receipt.parse unless the case
        # stops earlier); AdapterClient.call answers an operation the adapter lacks
        # locally, so without each of them the SDK does not run the whole case. A claim
        # built without them (older callers) needs the parse and format pair.
        needed = claim.operations or ("challenge.parse", "credential.format")
        return all(operation in capabilities for operation in needed)
    if claim.adapters is not None and adapter not in claim.adapters:
        return False
    if adapter in claim.crashed_adapters:
        return False
    if claim.operation is not None and claim.operation not in capabilities:
        return False
    if any(operation not in capabilities for operation in claim.operations):
        return False
    return version_applies is None or version_applies(claim, adapter)


def rejected_by_runner(claim: Claim, warnings: list[str]) -> bool:
    """Whether no SDK can pass this claim, so it is coverage for none of them.

    That is when the runner refuses the claim's scenario outright, for every adapter:
    its sdkVersions has unknown adapter keys or non-string values, or its adapters list
    names an unknown adapter; or when it refuses the whole file because another scenario
    in it is invalid, or the scenario calls an unmapped command, or a flow case's
    challenge_path matches no flow case (runner_error). Such a
    claim counts for no SDK and for no suite-wide total. Warns once per claim.
    """
    if claim.runner_error:
        message = f"{claim.source}: {claim.runner_error}; not counted as coverage"
        if message not in warnings:
            warnings.append(message)
        return True
    if not claim.sdk_versions and claim.adapters is None:
        return False
    scenario: dict[str, Any] = {}
    if claim.sdk_versions:
        scenario["sdkVersions"] = claim.sdk_versions
    if claim.adapters is not None:
        scenario["adapters"] = claim.adapters
    try:
        check_scenario(VectorRunner(), scenario)
    except ValueError as error:
        message = f"{claim.source}: {error}; not counted as coverage"
        if message not in warnings:
            warnings.append(message)
        return True
    return False


def version_checker(
    warnings: list[str], resolver: Callable[[str], str] | None = None
) -> VersionCheck:
    """Evaluate a claim's sdkVersions for an adapter the way the vector runner does.

    For an adapter, run_vector_file walks every scenario in the file. On each scenario
    that selects the adapter and pins a version for it, it resolves the adapter's SDK
    version (once, then cached) and matches it against the expression, which parses the
    expression. A resolver failure or a malformed expression raises out of the loop and
    aborts the whole file for that adapter only, before any scenario in it runs, so:

    - every expression in claim.file_constraints for the adapter must resolve and parse,
      whatever its match result, or no claim in the file counts for that adapter;
    - then the claim's own expression for the adapter, if any, must match.

    An adapter whose SDK version cannot be resolved (its toolchain is missing) is tried
    once and then counts none of the claims in files that pin it, with one warning.
    """
    runner = VectorRunner(sdk_version_resolver=resolver)
    unresolved: set[str] = set()

    def matches(adapter: str, expression: str, source: str) -> bool | None:
        """The match result, or None when the runner would raise and abort the file."""
        if adapter in unresolved:
            return None
        if adapter not in runner.sdk_versions:
            try:
                runner.sdk_versions[adapter] = runner.sdk_version_resolver(adapter)
            except (
                OSError,
                subprocess.CalledProcessError,
                RuntimeError,
                StopIteration,
                KeyError,
                ValueError,
            ):
                unresolved.add(adapter)
                warnings.append(
                    f"{adapter}: pinned SDK version unknown; claims in vector files that pin it are not counted"
                )
                return None
        try:
            return matches_constraint(runner.sdk_versions[adapter], expression)
        except ValueError as error:
            message = f"{source}: {error}; the runner aborts this file for {adapter}, so its claims are not counted for {adapter}"
            if message not in warnings:
                warnings.append(message)
            return None

    def applies(claim: Claim, adapter: str) -> bool:
        if rejected_by_runner(claim, warnings):
            return False
        source = claim.source.split("#", 1)[0]
        for expression in claim.file_constraints.get(adapter, ()):
            if matches(adapter, expression, source) is None:
                return False
        own = (claim.sdk_versions or {}).get(adapter)
        if own is None:
            return True
        return bool(matches(adapter, own, source))

    return applies


def excerpt(text: str) -> str:
    return (
        text
        if len(text) <= EXCERPT_LENGTH
        else text[: EXCERPT_LENGTH - 1].rstrip() + "…"
    )


def build_report(
    requirements: list[Requirement],
    claims: list[Claim],
    profiles: dict[str, dict[str, Any]],
    warnings: list[str],
    specs: dict[str, str | None],
    table_rows: dict[str, int] | None = None,
    version_applies: VersionCheck | None = None,
    draft_files: dict[str, str] | None = None,
    adapters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON report: per-draft counts, uncovered rules and profile coverage.

    draft_files lists every draft read (short name to file), so a draft without prose
    rules, such as one whose keywords are all in tables, still gets an entry, its table
    rows still count in the totals, and a profile claiming it does not call it unknown.
    A rule whose ID is a duplicate or another draft's (id_problem) is never covered.
    adapters maps every adapter the runner can run to its capabilities (all manifests
    under adapters/ when omitted); a vector claim no adapter runs covers nothing.
    """
    # Claims the runner refuses outright count nowhere: not in the suite-wide coverage,
    # the per-draft and total counts, or any SDK profile.
    # Every ID any claim cites, run or not: a typo'd or stale ID is worth reporting even on
    # a claim that covers nothing (a server_only flow case, a scenario no adapter runs, or
    # one the runner rejects), or it would hide behind the generic warning.
    cited: dict[str, list[str]] = {}
    for claim in claims:
        for req_id in claim.requirements:
            cited.setdefault(req_id, []).append(claim.source)
    claims = [claim for claim in claims if not rejected_by_runner(claim, warnings)]
    if adapters is None:
        adapters = load_capabilities()
    coverage: dict[str, list[str]] = {}
    for claim in claims:
        # A vector scenario counts suite-wide only when at least one adapter would run it,
        # as vector_runner decides per adapter: the scenario's adapters list selects it,
        # it advertises every operation the scenario calls, and the scenario's
        # sdkVersions (and its file's) let it run. Otherwise every adapter skips the
        # scenario and nothing checks the rule. A server_only flow case is never run:
        # flow_runner skips it (it only defines a route other cases reach), so it checks
        # nothing either. raw_http flow cases need no adapter, so they count suite-wide as
        # they are; client_flow and ordinary ones count only when some adapter runs them.
        if claim.flow_mode == "server_only":
            message = f"{claim.source}: server_only flow cases are not run; not counted as coverage"
            if message not in warnings:
                warnings.append(message)
            continue
        # An unsent case raises in perform_request before its first request leaves the
        # runner, so it checks nothing.
        if claim.flow_mode == "unsent":
            message = f"{claim.source}: its first request cannot be sent; not counted as coverage"
            if message not in warnings:
                warnings.append(message)
            continue
        # client_flow and ordinary flow cases drive an adapter too, so they need one that
        # takes part (claim_applies); only raw_http cases check the server alone.
        if claim.flow_mode in (None, "client_flow", "ordinary") and not any(
            claim_applies(claim, adapter, capabilities, version_applies)
            for adapter, capabilities in sorted(adapters.items())
        ):
            message = f"{claim.source}: no adapter runs it (adapters, capabilities or sdkVersions); not counted as coverage"
            if message not in warnings:
                warnings.append(message)
            continue
        for req_id in claim.requirements:
            coverage.setdefault(req_id, []).append(claim.source)
    known = {
        requirement.req_id
        for requirement in requirements
        if requirement.req_id and requirement.id_problem != "malformed"
    }
    unknown = sorted(req_id for req_id in cited if req_id not in known)

    drafts: dict[str, dict[str, Any]] = {}

    def draft_entry(draft: str, file: str) -> dict[str, Any]:
        return drafts.setdefault(
            draft,
            {
                "draft": draft,
                "file": file,
                "rules": 0,
                "tagged": 0,
                "covered": 0,
                "table_rows": (table_rows or {}).get(draft, 0),
                "uncovered": [],
            },
        )

    for draft, file in (draft_files or {}).items():
        draft_entry(draft, file)
    for requirement in requirements:
        entry = draft_entry(requirement.draft, requirement.file)
        entry["rules"] += 1
        if requirement.req_id and requirement.id_problem != "malformed":
            entry["tagged"] += 1
        if requirement.req_id in coverage and not requirement.id_problem:
            entry["covered"] += 1
        else:
            uncovered = {
                "key": requirement.key,
                "line": requirement.line,
                "section": requirement.section,
                "keywords": requirement.keywords,
                "excerpt": excerpt(requirement.text),
            }
            if requirement.id_problem:
                uncovered["id_problem"] = requirement.id_problem
            entry["uncovered"].append(uncovered)

    profile_report: dict[str, Any] = {}
    for adapter, manifest in sorted(profiles.items()):
        applicable = {
            req_id
            for claim in claims
            if claim_applies(claim, adapter, manifest["capabilities"], version_applies)
            for req_id in claim.requirements
        }
        per_draft: dict[str, dict[str, int]] = {}
        for draft in manifest["profile"]:
            rules = [
                requirement
                for requirement in requirements
                if requirement.draft == draft
            ]
            per_draft[draft] = {
                "rules": len(rules),
                "covered": sum(
                    1
                    for requirement in rules
                    if requirement.req_id in applicable and not requirement.id_problem
                ),
            }
        profile_report[adapter] = {
            "profile": manifest["profile"],
            "drafts": per_draft,
            "unknown_drafts": sorted(
                draft for draft in manifest["profile"] if draft not in drafts
            ),
        }

    countable = {
        requirement.req_id
        for requirement in requirements
        if requirement.req_id and not requirement.id_problem
    }
    totals = {
        "rules": len(requirements),
        "tagged": sum(entry["tagged"] for entry in drafts.values()),
        "covered": sum(entry["covered"] for entry in drafts.values()),
        "table_rows": sum(entry["table_rows"] for entry in drafts.values()),
    }
    return {
        "specs": specs,
        "totals": totals,
        "drafts": sorted(drafts.values(), key=lambda entry: entry["file"]),
        # Only IDs that count as covered above: defined once, well formed and in their own
        # draft. A claim citing any other ID shows up under unknown_references or the
        # warnings, never as coverage the totals don't count.
        "coverage": {
            req_id: sorted(sources)
            for req_id, sources in sorted(coverage.items())
            if req_id in countable
        },
        "unknown_references": unknown,
        # Every claim citing each unknown ID, including claims that cover nothing.
        "unknown_reference_sources": {
            req_id: sorted(set(cited[req_id])) for req_id in unknown
        },
        "profiles": profile_report,
        "warnings": warnings,
    }


# ASCII punctuation that Markdown can read as syntax inside a line: emphasis, code,
# links and images, HTML and entities, strikethrough and table pipes. Line-start syntax
# (list markers, numbered lists, headings) needs no escape, as render_markdown never
# starts a line with a value; '#' is still escaped so a heading cannot gain levels.
MD_SPECIAL = frozenset("\\" + "\x60" + "*_[]<>#!|~&")


def md_text(value: object) -> str:
    """Return value as one line of literal Markdown text, safe in a table cell or heading.

    Draft names, paths, keys, excerpts and warnings come from the specs checkout, so
    a newline could start a fake table row or heading, and a pipe, bracket, backtick
    or other Markdown punctuation could change the table or add links. Line breaks
    become spaces and every ASCII punctuation character Markdown can treat as syntax
    is backslash-escaped (CommonMark section 2.4 lets any ASCII punctuation be escaped).
    """
    text = " ".join(str(value).split())
    return "".join("\\" + ch if ch in MD_SPECIAL else ch for ch in text)


def md_code(value: object) -> str:
    """Return value as a single-line Markdown code span that it cannot break out of.

    The fence is one backtick longer than the longest backtick run inside, and a
    space pads a value that starts or ends with a backtick or is blank (CommonMark
    section 6.1), so a backtick in a key cannot close the span early. GitHub tables
    still split a cell at a pipe inside a code span, so callers use it outside tables.
    """
    text = " ".join(str(value).split())
    longest = max((len(run) for run in re.findall("\x60+", text)), default=0)
    fence = "\x60" * (longest + 1)
    pad = " " if text.startswith("\x60") or text.endswith("\x60") or not text else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report as a Markdown summary with the uncovered rules per draft."""
    specs = report["specs"]
    totals = report["totals"]
    lines = [
        "# MPP requirement gap report",
        "",
        f"Specs: {md_text(specs.get('source'))} at {md_text(specs.get('commit') or 'unknown commit')}.",
        "",
        f"{totals['rules']} rules, {totals['tagged']} tagged with an ID, {totals['covered']} covered by a vector or flow.",
        f"{totals['table_rows']} table rows also carry a keyword; they are counted, not extracted.",
        "",
        "| Draft | Rules | Tagged | Covered | Table rows |",
        "|-------|------:|-------:|--------:|-----------:|",
    ]
    for entry in report["drafts"]:
        lines.append(
            f"| {md_text(entry['draft'])} | {entry['rules']} | {entry['tagged']} | {entry['covered']} | {entry['table_rows']} |"
        )
    if report["profiles"]:
        lines += [
            "",
            "## SDK profiles",
            "",
            "| Adapter | Draft | Rules | Covered |",
            "|---------|-------|------:|--------:|",
        ]
        for adapter, entry in report["profiles"].items():
            for draft, counts in entry["drafts"].items():
                lines.append(
                    f"| {md_text(adapter)} | {md_text(draft)} | {counts['rules']} | {counts['covered']} |"
                )
    if report["unknown_references"]:
        lines += ["", "## Unknown requirement IDs", ""]
        lines += [
            f"- {md_code(req_id)}: {', '.join(md_text(source) for source in report['unknown_reference_sources'][req_id])}"
            for req_id in report["unknown_references"]
        ]
    if report["warnings"]:
        lines += ["", "## Warnings", ""]
        lines += [f"- {md_text(warning)}" for warning in report["warnings"]]
    lines += ["", "## Uncovered rules"]
    for entry in report["drafts"]:
        if not entry["uncovered"]:
            continue
        lines += ["", f"### {md_text(entry['draft'])} ({md_text(entry['file'])})", ""]
        for rule in entry["uncovered"]:
            text = md_text(rule["excerpt"])
            problem = f" [{md_text(rule['id_problem'])} ID, not credited]" if rule.get("id_problem") else ""
            lines.append(f"- {md_code(rule['key'])} (line {rule['line']}){problem}: {text}")
    return "\n".join(lines) + "\n"


def git_blob_sha(data: bytes) -> str:
    """Return the git blob id of data, as 'git hash-object' computes it without filters."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# The file that pins the checkout's kramdown-rfc version (locked_kramdown_rfc_version).
LOCKFILE = "Gemfile.lock"


def git_commit(path: Path) -> str | None:
    """Return the commit the specs at path were read from, or None if unknown.

    None when path is not the root of a git checkout (its HEAD would belong to an
    enclosing repository). A "-dirty" suffix when the drafts the report reads
    differ from HEAD's: the specs/**/*.md files draft discovery finds in the
    working tree, and Gemfile.lock (which pins the renderer), are compared, by path
    and blob id, with the ones in HEAD's tree.
    Comparing the files themselves rather than parsing 'git status' also covers
    untracked, gitignored, oddly named and nested-repository files.

    Symlinked drafts are left out on both sides; draft discovery refuses them. A file
    whose raw bytes do not hash to HEAD's blob is hashed again by 'git hash-object',
    which applies the checkout's clean filters (such as CRLF conversion under
    core.autocrlf or .gitattributes) the way 'git add' would, so a clean checkout with
    converted line endings is not called dirty.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout

    try:
        if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != path.resolve():
            return None
        commit = git("rev-parse", "HEAD").strip()
        # "<mode> <type> <sha>\t<path>" per entry, NUL-separated so any name is exact.
        head: dict[str, str] = {}
        for entry in git("ls-tree", "-r", "-z", "HEAD", "--", "specs", LOCKFILE).split("\0"):
            if not entry:
                continue
            meta, name = entry.split("\t", 1)
            mode, kind, sha = meta.split()
            if (name.endswith(".md") or name == LOCKFILE) and kind == "blob" and mode != "120000":
                head[name] = sha
        working: dict[str, str] = {}
        # Gemfile.lock pins the kramdown-rfc version the drafts render with, so a changed
        # or missing one makes the checkout dirty too.
        for file in [*(path / "specs").rglob("*.md"), path / LOCKFILE]:
            if file.is_symlink() or not file.is_file():
                continue
            name = file.relative_to(path).as_posix()
            sha = git_blob_sha(file.read_bytes())
            if name in head and head[name] != sha:
                sha = git("hash-object", "--", name).strip()
            working[name] = sha
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None
    if not commit:
        return None
    return f"{commit}-dirty" if working != head else commit


def specs_fingerprint(specs_dir: Path) -> str:
    """Return a sha256 over the path and bytes of every draft draft_paths finds.

    generate compares it before and after reading the drafts. The commit label alone
    cannot show a change there: every modified state under one HEAD is '<HEAD>-dirty'.
    A draft that cannot be read hashes as a marker, so it differs from its contents.
    """
    digest = hashlib.sha256()
    for path in draft_paths(specs_dir):
        name = path.relative_to(specs_dir).as_posix().encode("utf-8")
        try:
            data = path.read_bytes()
        except OSError:
            data = None
        digest.update(hashlib.sha256(name).digest())
        digest.update(b"\0" if data is None else b"\1" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def clone_specs(destination: Path, ref: str) -> Path:
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            ref,
            SPECS_REPOSITORY,
            str(destination),
        ],
        check=True,
    )
    return destination


def generate(
    specs_dir: Path, source: str, render: Renderer | None = None
) -> dict[str, Any]:
    """Build the report for one mpp-specs checkout against this suite's fixtures.

    render defaults to kramdown-rfc on PATH, checked against the checkout's pinned
    version before any draft is read.
    """
    if render is None:
        kramdown = KramdownRfc()
        kramdown.require(locked_kramdown_rfc_version(specs_dir))
        render = kramdown
    # Sample the checkout's commit and the drafts' exact contents before and after
    # reading them: if either changed in between, the rules counted may not match the
    # commit named, so refuse to name one rather than attest the wrong source. The
    # fingerprint catches a draft that changes again in an already dirty checkout, whose
    # label stays '<HEAD>-dirty'.
    fingerprint_before = specs_fingerprint(specs_dir)
    commit_before = git_commit(specs_dir)
    requirements, warnings, table_rows, draft_files = extract_all(specs_dir, render)
    commit = git_commit(specs_dir)
    if commit != commit_before:
        raise ValueError(
            f"{specs_dir} changed while it was read ({commit_before} -> {commit}); run again"
        )
    if specs_fingerprint(specs_dir) != fingerprint_before:
        raise ValueError(f"{specs_dir} drafts changed while they were read; run again")
    claims = load_claims()
    profiles = load_profiles()
    adapters = load_capabilities()
    specs: dict[str, str | None] = {"source": source, "commit": commit}
    return build_report(
        requirements,
        claims,
        profiles,
        warnings,
        specs,
        table_rows,
        version_applies=version_checker(warnings),
        draft_files=draft_files,
        adapters=adapters,
    )


def checked_renderer(specs_dir: Path, command: list[str] | None) -> KramdownRfc:
    """Return a kramdown-rfc renderer checked against the version specs_dir pins.

    A missing specs directory is reported first, as before kramdown-rfc was needed.
    """
    draft_paths(specs_dir)
    kramdown = KramdownRfc(command)
    kramdown.require(locked_kramdown_rfc_version(specs_dir))
    return kramdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--specs",
        type=Path,
        help="mpp-specs checkout; clones tempoxyz/mpp-specs when omitted",
    )
    parser.add_argument(
        "--ref", default="main", help="branch or tag to clone when --specs is omitted"
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=CONFORMANCE_DIR / "gap-report.json",
        help="JSON report path",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=CONFORMANCE_DIR / "gap-report.md",
        help="Markdown summary path",
    )
    parser.add_argument(
        "--kramdown-rfc",
        type=shlex.split,
        help="command that renders a draft read from stdin to XML (default: kramdown-rfc, "
        "at the version the checkout's Gemfile.lock pins), such as "
        "'docker run --rm -i ietf-spec-tools kramdown-rfc'",
    )
    args = parser.parse_args(argv)

    try:
        if args.specs:
            specs_dir = args.specs.resolve()
            render = checked_renderer(specs_dir, args.kramdown_rfc)
            report = generate(specs_dir, str(args.specs), render)
        else:
            with tempfile.TemporaryDirectory(prefix="mpp-specs-") as directory:
                specs_dir = clone_specs(Path(directory) / "mpp-specs", args.ref)
                render = checked_renderer(specs_dir, args.kramdown_rfc)
                report = generate(specs_dir, f"{SPECS_REPOSITORY}@{args.ref}", render)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    totals = report["totals"]
    print(
        f"{totals['rules']} rules, {totals['tagged']} tagged, {totals['covered']} covered; "
        f"wrote {args.json} and {args.markdown}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
