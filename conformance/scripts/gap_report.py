#!/usr/bin/env python3
"""Report which BCP 14 requirements in the MPP specifications conformance covers.

Reads every `specs/**/*.md` draft in a tempoxyz/mpp-specs checkout (a fresh
clone of main when --specs is omitted) and extracts each paragraph or list item
that contains an RFC 2119/8174 keyword in capitals. A requirement's key is the
`{: #req-<draft>-<nnn>}` IAL right after it when present, otherwise
`<draft>#<section>:<sha256 prefix>`. Vector scenarios and flow cases claim
requirement IDs with an optional `requirements` array; adapter manifests claim
drafts with an optional `profile`. The report lists, per draft, how many rules
there are, how many carry an ID and how many a vector or flow covers.
"""

from __future__ import annotations

import argparse
import hashlib
import html.entities
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness import (
    ADAPTERS_DIR,
    COMMAND_TO_OPERATION,
    CONFORMANCE_DIR,
    FLOW_CASES_PATH,
    VECTORS_DIR,
    load_flow_cases,
    load_json,
    load_vector,
    validate_value,
)
from vector_runner import VectorRunner
from version_constraints import matches_constraint

SPECS_REPOSITORY = "https://github.com/tempoxyz/mpp-specs"
EXCERPT_LENGTH = 160

# Longest alternatives first so "MUST NOT" is not read as "MUST". keywords_in collapses
# whitespace before matching, so a two-word keyword hard-wrapped across lines ("MUST" at
# the end of one line, "NOT" at the start of the next) still matches as "MUST NOT".
KEYWORD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:MUST NOT|SHALL NOT|SHOULD NOT|NOT RECOMMENDED|MUST|REQUIRED|SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)(?![A-Za-z0-9])"
)
# A keyword quoted on its own, as in the boilerplate 'The key words "MUST", ...', names
# the keyword rather than stating a rule. Only that exact form is dropped, the way
# mpp-specs' scripts/lint_requirements.py does (QUOTED_KEYWORDS), so both tools agree on
# which blocks are rules: a keyword in a longer quoted phrase ("clients MUST retry") or
# in single quotes still counts.
QUOTED_KEYWORD_RE = re.compile(
    r'"(?:MUST NOT|SHALL NOT|SHOULD NOT|NOT RECOMMENDED|MUST|REQUIRED|SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)"'
)
# req-<draft>-<nnn>, where <draft> is a short draft name: lowercase words joined by single
# hyphens, so an empty component such as req-core--001 is malformed. The schemas use the
# same pattern.
REQ_ID_RE = re.compile(r"^req-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{3}$")
# A block IAL alone on its line, such as '{: #req-core-001}'. kramdown extension tags
# ('{::boilerplate ...}', '{:/}') match too; ial_req_id reads no ID from them.
# The body may hold '\}', an escaped brace, as kramdown's ALD_ANY_CHARS allows, so
# '{: title="a\}" id="req-core-001"}' is one IAL (checked with kramdown-rfc).
IAL_LINE_RE = re.compile(r"^\s*\{:(?P<body>(?:\\\}|[^}])*)\}\s*$")
# A setext heading's underline: kramdown makes the single line of text right before it,
# at a block boundary, a heading rather than a paragraph.
SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
# An IAL at the very start of a list item's text, '- {: #req-core-001} Clients MUST ...':
# kramdown-rfc gives the list item that ID.
LEADING_IAL_RE = re.compile(r"\{:(?P<body>[^{}]*)\}[ \t]*")
# An IAL at the end of a line of text, after a space or punctuation rather than right
# after a span element (*em*, `code`, [link]). kramdown-rfc renders it as literal
# text and the rule gets no ID (checked with kramdown-rfc 1.7.39), so the report warns
# rather than reading an ID from it. A backslash-escaped '\{:' is literal text as well.
TRAILING_IAL_RE = re.compile(r"(?<![\\*_\x60\])>])\{:(?P<body>[^{}]*)\}\s*$")
# One attribute of an IAL body, scanned the way kramdown's parse_attribute_list does:
# key="value" (or 'value'), one or more #id and .class names run together, or a bare
# reference name ('ref': an attribute list definition's name), each separated by
# whitespace. A '#name' token and a quoted id="..." (or id='...') value set the ID, the
# last one winning; '#req-...' inside another attribute's value, such as
# title="#req-core-001", sets nothing, and kramdown ignores an unquoted id=req-core-001.
# Inside the quotes a backslash is matched only as the start of an escape pair and every
# other character only on its own ('[^\\]'), so each character has exactly one way to
# match: a value that never closes fails in linear time instead of backtracking
# exponentially over a long backslash run (ReDoS).
IAL_TOKEN_RE = re.compile(
    r"""(?:^|(?<=\s))(?:(?P<key>\w[\w-]*)=(?P<quote>["'])(?P<value>(?:\\.|(?!(?P=quote))[^\\\n])*)(?P=quote)|(?P<names>(?:[#.]\w[\w-]*)+)|(?P<ref>\w[\w-]*))(?=\s|$)"""
)
IAL_ID_NAME_RE = re.compile(r"#(\w[\w-]*)")
# The escapes kramdown removes from a quoted IAL value: a backslash before '}' or the quote.
IAL_VALUE_ESCAPE_RE = re.compile(r"\\([}\"'])")
# '{:name: attributes}' is an attribute list definition: it names attributes for later
# reference and attaches nothing to the block before it.
ALD_RE = re.compile(r"^\w[\w-]*:")
# A whole definition line's name and attributes, as kramdown reads it (ALD_START): an IAL
# naming it ('{: reqdef}') gets those attributes, wherever in the draft it is defined.
ALD_DEF_RE = re.compile(r"^\s*\{:(?P<name>\w[\w-]*):(?P<attrs>(?:\\\}|[^}])+)\}\s*$")
# Headings are parsed in two linear steps (see parse_heading) rather than with one regex
# whose lazy title and optional whitespace runs backtrack on long whitespace.
HEADING_RE = re.compile(r"^#{1,6}\s+(?P<rest>.*)$")
HEADING_ANCHOR_RE = re.compile(r"\{#(?P<anchor>[^{}\s]+)\}$")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
DEFINITION_RE = re.compile(r"^\s*:\s+")
# kramdown-rfc fences code with ~~~ only; a ``` pair renders as inline verbatim, whose
# keywords keywords_in already ignores. A fence line indented 4 or more is code.
FENCE_RE = re.compile(r"^( *)(~{3,})(.*)$")
# Every blockquote marker at the start of a line: kramdown parses a quote's content as
# blocks again, so '> > text' (or '>> text') is a quote nested in a quote, and a block
# IAL inside it ('> > {: #req-core-001}') tags the nested paragraph.
QUOTE_RE = re.compile(r"^(?: {0,3}> ?)+")
# A table delimiter row, with or without outer pipes: | --- | :---: | or --- | ---
TABLE_DELIMITER_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
# kramdown starts a table at a block boundary on a line with a pipe that is neither
# backslash-escaped nor inside a code span (TABLE_PIPE_CHECK in kramdown's table.rb);
# neither a leading pipe nor a delimiter row is needed.
UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")
# kramdown-rfc splits a sectionized draft (one whose first line starts with '---') into
# sections on these lines before it parses any Markdown (RE_SECTION in its command.rb):
# '--- name' pushes section 'name' (a trailing '-' only keeps the section out of
# Markdown) and a bare '---' pops back to the previous section, whose text the lines
# after it then continue.
SECTION_RE = re.compile(r"^---(?: +(?P<name>\w+)-?)? *$")
# The sections kramdown-rfc2629.erb renders. Text in any other section is never
# published: 'fluff' is kramdown-rfc's commented-out section, other names are left over
# with a warning, and text after a '---' that empties the stack belongs to no section.
RENDERED_SECTION_RE = re.compile(
    r"^(?:abstract|middle|back|contributor|normative|informative|(?:to_be_removed_)?note_\w*)$"
)
# kramdown comments: an HTML comment, or a {::comment} extension that '{:/comment}' or
# '{:/}' closes. Either can open and close mid-line or run over several lines. A run of
# backticks is matched too, so strip_comments can step over code spans.
COMMENT_CLOSERS = {"<!--": ("-->",), "{::comment}": ("{:/comment}", "{:/}")}
COMMENT_OPEN_RE = re.compile(r"<!--|\{::comment\}|(?<!\\)\x60+")
BACKTICKS_RE = re.compile(r"(?<!\\)\x60+")
# A link's destination (and title) and an autolink are attributes, not text: a capital
# keyword in a URL, as in [spec](https://example.com/MUST), is not a rule.
LINK_DESTINATION_RE = re.compile(r"\]\([^()\s]*(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
AUTOLINK_RE = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]*:[^\s<>]*>")
# A character reference as kramdown 2.4 reads one (HTML_ENTITY_RE in its parser/html.rb):
# the ';' is required and a hex reference takes a lowercase 'x', so '&#77;UST' and
# '&#x4d;UST' render as MUST while '&#77UST' and '&#X4d;UST' stay literal text
# (checked with kramdown-rfc 1.7.39).
CHAR_REF_RE = re.compile(r"&([\w:][\-\w.:]*);|&#(\d+);|&#x([0-9a-fA-F]+);")


def decode_char_refs(text: str) -> str:
    """Replace each character reference with the character kramdown renders for it.

    An unknown name (such as '&MUST;') or an out-of-range code point is left as it is.
    """

    def decode(match: re.Match[str]) -> str:
        name, decimal, hexadecimal = match.groups()
        if name is not None:
            return html.entities.html5.get(name + ";", match.group())
        try:
            return chr(int(decimal) if decimal is not None else int(hexadecimal, 16))
        except (ValueError, OverflowError):
            return match.group()

    return CHAR_REF_RE.sub(decode, text)


def parse_heading(line: str) -> tuple[str, str | None] | None:
    """Return (title, anchor) for a Markdown heading line, or None."""
    match = HEADING_RE.match(line)
    if not match:
        return None
    rest = match.group("rest").strip()
    anchor = HEADING_ANCHOR_RE.search(rest)
    if anchor:
        return rest[: anchor.start()].rstrip(), anchor.group("anchor")
    return rest, None


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
    """Slug a heading the way kramdown-rfc derives an anchor from it."""
    text = re.sub(r"[\x60*_]", "", text).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "section"


def normalize(text: str) -> str:
    """Collapse whitespace and drop list markers so reflowing keeps the hash."""
    text = LIST_ITEM_RE.sub("", text, count=1)
    text = DEFINITION_RE.sub("", text, count=1)
    return " ".join(text.split())


def code_span_end(text: str, start: int, end: int) -> int | None:
    """Return where the code span opened by the backtick run text[start:end] ends.

    Mirrors kramdown's parse_codespan (kramdown 2.4): the span closes at the first later
    occurrence of the same backtick string, and None means the run is literal text:
    either nothing closes it, or it is a single backtick with whitespace (or the start
    of the text) before it and whitespace after it.
    """
    run = end - start
    if (
        run == 1
        and (start == 0 or text[start - 1].isspace())
        and end < len(text)
        and text[end].isspace()
    ):
        return None
    close = text.find("\x60" * run, end)
    return None if close < 0 else close + run


def strip_code_spans(text: str) -> str:
    """Replace each kramdown code span with a space; its text is verbatim, never a rule.

    A span opens at a run of backticks that is not backslash-escaped and ends as
    code_span_end says, so a double-backtick span may hold a single backtick, and a run
    that opens no span is literal text and stays. Pairing single backticks with one
    regex instead would misalign after such a span or a stray backtick and delete
    ordinary text, keywords included, up to the next code span.
    """
    pieces: list[str] = []
    position = 0
    while True:
        match = BACKTICKS_RE.search(text, position)
        if not match:
            pieces.append(text[position:])
            return "".join(pieces)
        close = code_span_end(text, match.start(), match.end())
        if close is None:
            pieces.append(text[position : match.end()])
            position = match.end()
            continue
        pieces.append(text[position : match.start()])
        pieces.append(" ")
        position = close


def keywords_in(text: str) -> list[str]:
    """Return the BCP 14 keywords in text as a reader of the rendered draft sees them.

    Ignored: keywords in code spans, in link destinations or autolinks, and a keyword
    double-quoted on its own ("MUST", see QUOTED_KEYWORD_RE). Character references in
    the remaining text are decoded first, as the rendered draft shows them ('&#77;UST'
    is MUST, '&quot;MUST&quot;' a quoted keyword); inside a code span they stay literal.
    Whitespace, newlines included, is collapsed so a keyword pair wrapped across lines
    still reads as one keyword ("MUST NOT", not "MUST").
    """
    visible = strip_code_spans(text)
    visible = LINK_DESTINATION_RE.sub("]", visible)
    visible = AUTOLINK_RE.sub(" ", visible)
    visible = decode_char_refs(visible)
    visible = " ".join(visible.split())
    visible = QUOTED_KEYWORD_RE.sub(" ", visible)
    return sorted(set(KEYWORD_RE.findall(visible)))


def is_table_line(text: str) -> bool:
    """Whether kramdown reads this line as a table row: an unescaped pipe outside code."""
    return bool(UNESCAPED_PIPE_RE.search(strip_code_spans(text)))


def table_row_keywords(text: str) -> int:
    """1 when a table row (not a delimiter row) has a BCP 14 keyword, otherwise 0."""
    return 1 if keywords_in(text) and not TABLE_DELIMITER_RE.match(text) else 0


def strip_comments(
    line: str, closers: tuple[str, ...] | None
) -> tuple[str, tuple[str, ...] | None, bool]:
    """Remove kramdown comments (HTML and {::comment}) from one line.

    closers holds the closing delimiters of a comment left open by an earlier line, or
    None. Returns the line's visible text, the closers of a comment still open at its
    end, and whether the line held any comment. A comment may open and close anywhere
    on a line, so text before an opener or after a closer stays visible. Code spans on
    the line are copied as they are: a '<!--' quoted in backticks opens nothing.
    """
    visible: list[str] = []
    position = 0
    touched = closers is not None
    while position < len(line):
        if closers is not None:
            ends = [
                (index, closer)
                for closer in closers
                if (index := line.find(closer, position)) >= 0
            ]
            if not ends:
                return "".join(visible), closers, True
            index, closer = min(ends)
            position = index + len(closer)
            closers = None
            continue
        match = COMMENT_OPEN_RE.search(line, position)
        if not match:
            visible.append(line[position:])
            break
        token = match.group()
        if token.startswith("\x60"):
            close = code_span_end(line, match.start(), match.end())
            end = close if close is not None else match.end()
            visible.append(line[position:end])
            position = end
            continue
        visible.append(line[position : match.start()])
        touched = True
        closers = COMMENT_CLOSERS[token]
        position = match.end()
    return "".join(visible), closers, touched


def ial_id(body: str, alds: dict[str, list[str]] | None = None) -> str | None:
    """Return the ID an IAL body gives its element, requirement or not, or None.

    kramdown writes every '#name' token and quoted id="..." value to the same attribute
    in order, so the last one is the element's ID: '{: #req-core-001 id="note"}' gives
    it 'note'. Only real tokens count (IAL_TOKEN_RE). A bare name refers to an attribute
    list definition in alds (name to its definitions' attribute strings, in draft order),
    whose IDs kramdown applies before the IAL's own, so an own ID still wins. Extension
    tags ('{::name}', '{:/name}') and definitions themselves ('{:name: ...}') set no ID.
    """
    if body.startswith((":", "/")) or ALD_RE.match(body):
        return None
    return final_id(body, alds or {}, {})


# kramdown resolves definitions without a depth limit; past this many nested references
# the report stops with an error instead of exhausting Python's recursion limit.
MAX_ALD_DEPTH = 100
# memo's marker for a definition being expanded, distinct from one that sets no ID (None)
# and one that sets an empty ID ('').
_EXPANDING = object()


def final_id(
    attrs: str,
    alds: dict[str, list[str]],
    memo: dict[str, object],
    depth: int = 0,
) -> str | None:
    """Return the last ID an attribute string sets, in the order kramdown applies them.

    Mirrors mpp-specs' scripts/lint_requirements.py (ial_ids): referenced definitions'
    IDs first, then the string's own '#name' and id="..." IDs, so an own ID wins over
    any referenced one, and among references the last one that sets an ID wins. An
    explicit id="" is an ID too: kramdown renders it as anchor="", which replaces any
    earlier ID, so it returns '' (no ID set at all is None).

    Only the final ID matters, so each definition name is resolved once and kept in
    memo: a definition reachable along many paths ('{:a: b b}', '{:b: c c}', ...)
    costs one expansion, not one per path, which keeps a chain of n definitions linear
    instead of 2^n. A name being expanded maps to _EXPANDING, so a reference cycle
    resolves to nothing instead of recursing. A chain deeper than MAX_ALD_DEPTH raises
    ValueError, which the report shows as an error rather than a traceback.
    """
    if depth > MAX_ALD_DEPTH:
        raise ValueError(f"attribute list definitions nest more than {MAX_ALD_DEPTH} deep")
    own: str | None = None
    referenced: str | None = None
    for token in IAL_TOKEN_RE.finditer(attrs):
        if token.group("key") == "id":
            own = IAL_VALUE_ESCAPE_RE.sub(r"\1", token.group("value"))
        elif token.group("names"):
            names = IAL_ID_NAME_RE.findall(token.group("names"))
            if names:
                own = names[-1]
        elif (ref := token.group("ref")) in alds:
            if ref not in memo:
                memo[ref] = _EXPANDING
                resolved: str | None = None
                for definition in alds[ref]:
                    found = final_id(definition, alds, memo, depth + 1)
                    if found is not None:
                        resolved = found
                memo[ref] = resolved
            value = memo[ref]
            if isinstance(value, str):
                referenced = value
    return own if own is not None else referenced


def ial_req_id(body: str, alds: dict[str, list[str]] | None = None) -> str | None:
    """Return the requirement ID an IAL body sets, or None when it sets none.

    That is its final ID (ial_id, with alds' definitions expanded) when it starts with
    'req-', so a later non-requirement ID in the same IAL overrides an earlier
    requirement ID. Whether the rest matches req-<draft>-<nnn> is checked separately (a
    malformed ID is reported, not credited).
    """
    identifier = ial_id(body, alds)
    return identifier if identifier and identifier.startswith("req-") else None


def extract_requirements(
    path: Path, relative: str, alds: dict[str, list[str]] | None = None
) -> tuple[list[Requirement], list[str], int]:
    """Extract every requirement in one draft, with warnings and the keyword table rows.

    The draft is read the way kramdown-rfc renders it, so that a rule is counted only
    when it appears as prose in the published draft:

    - Sections: a draft whose first line starts with '---' is split into sections on
      '---' lines first (SECTION_RE), and only text in a rendered section is read
      (RENDERED_SECTION_RE). The first section is the YAML front matter.
    - Code: ~~~ fences and indented code blocks are skipped.
    - Comments: HTML and {::comment} comments are removed wherever they open and close.
      A line holding only comment text neither adds text nor ends a paragraph: kramdown
      keeps a comment between two lines of a paragraph inside that paragraph.
    - Tables: a block that starts at a block boundary with a line holding an unescaped
      pipe is a table, provided the rows are followed by a block boundary (a blank line,
      a block IAL or the end of the section); otherwise kramdown reads the rows as a
      paragraph. Table rows (field tables with REQUIRED or OPTIONAL columns) cannot carry
      an IAL each, so they are counted rather than extracted; schemas cover them.
    - IDs: a rule's ID comes from a block IAL on the line right after it, or from an IAL
      at the very start of a list item's text. One at the end of a text line renders as
      literal text in kramdown-rfc, so it sets no ID and is warned about. An IAL naming
      an attribute list definition ('{:reqdef: #req-core-001}' defined outside code and
      comments) gets that definition's IDs. kramdown resolves those references after
      parsing, so a definition after the IAL counts too: when the draft defines any, it
      is read a second time with all of them (alds; callers leave it None).
    """
    draft = draft_short_name(path.name)
    # Split only where kramdown does: it turns CRLF and CR into LF, then reads LF-ended
    # lines. str.splitlines() would also split at U+2028, U+0085, form feed and the like,
    # which kramdown leaves inside a line, turning an inline '{: #id}' into a block IAL.
    lines = re.sub(r"\r\n?", "\n", path.read_text(encoding="utf-8")).split("\n")
    if lines and not lines[-1]:
        lines.pop()  # the final newline ends the last line rather than starting another
    requirements: list[Requirement] = []
    warnings: list[str] = []
    section = "abstract"
    unit: list[str] = []
    unit_line = 0
    fence: str | None = None
    # The closing delimiters of a comment still open from an earlier line, or None.
    comment: tuple[str, ...] | None = None
    table_rows = 0
    # Rows of a table kramdown may yet back out of: (line number, the line as a unit
    # holds it, the row's text). They count as a table once a block boundary follows
    # them, or become a paragraph when another line of text does.
    pending: list[tuple[int, str, str]] = []
    pending_in_item = False
    # Definitions this reading finds, and the ones IDs are resolved against: none on the
    # first reading, every one in the draft on the second.
    found_alds: dict[str, list[str]] = {}
    known_alds = alds or {}
    # Block IALs right after the current unit, one per line, not yet applied. kramdown
    # applies each consecutive IAL line to the same block, so the block's ID is the last
    # one any of them sets: '{: #req-core-001}' then '{: #note}' leaves it 'note', with
    # no requirement ID. The unit is flushed once the run of IAL lines ends.
    ial_bodies: list[str] = []

    def flush(req_id: str | None = None) -> None:
        nonlocal unit
        if not unit:
            return
        text = "\n".join(unit)
        unit = []
        item = LIST_ITEM_RE.match(text)
        if item:
            leading = LEADING_IAL_RE.match(text, item.end())
            if leading:
                # '- {: #req-...} text': kramdown-rfc gives the list item this ID, and it
                # keeps it. A block IAL on the line after the item goes on the enclosing
                # list instead (checked with kramdown-rfc 1.7.39), so a requirement ID
                # there must not replace the item's: warn and keep the leading one.
                leading_id = ial_req_id(leading.group("body"), known_alds)
                if leading_id:
                    if req_id and req_id != leading_id:
                        warnings.append(
                            f"{relative}:{unit_line}: {req_id} follows a list item that already "
                            f"has {leading_id}; kramdown-rfc puts it on the list, not the item"
                        )
                    req_id = leading_id
                text = text[: item.end()] + text[leading.end() :]
        trailing = TRAILING_IAL_RE.search(text)
        trailing_id = ial_req_id(trailing.group("body"), known_alds) if trailing else None
        if trailing_id:
            warnings.append(
                f"{relative}:{unit_line}: {trailing_id} ends a line of text, "
                "where kramdown-rfc renders it as text, so it tags nothing; put it on the next line"
            )
        found = keywords_in(text)
        if not found:
            if req_id:
                warnings.append(
                    f"{relative}:{unit_line}: {req_id} tags text without a BCP 14 keyword"
                )
            return
        normalized = normalize(text)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        malformed = bool(req_id) and not REQ_ID_RE.match(req_id)
        if malformed:
            warnings.append(
                f"{relative}:{unit_line}: {req_id} does not match req-<draft>-<nnn>; not counted as tagged or covered"
            )
        requirements.append(
            Requirement(
                draft=draft,
                file=relative,
                line=unit_line,
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

    def apply_ials() -> None:
        """Flush the unit with the ID its run of block IALs gives it, if one is pending."""
        if not ial_bodies:
            return
        final = None
        for body in ial_bodies:
            found = ial_id(body, known_alds)
            if found is not None:  # an empty id="" replaces an earlier ID too
                final = found
        ial_bodies.clear()
        flush(final if final and final.startswith("req-") else None)

    def is_block_ial(line: str) -> bool:
        """Whether a line is a block IAL that sets attributes (not an extension or ALD)."""
        match = IAL_LINE_RE.match(line)
        if not match:
            return False
        body = match.group("body")
        return not body.startswith((":", "/")) and not ALD_RE.match(body)

    def commit_table() -> None:
        nonlocal table_rows
        table_rows += sum(table_row_keywords(row) for _, _, row in pending)
        pending.clear()

    def revert_table() -> None:
        # No block boundary after the rows: kramdown backs out of the table and parses
        # its lines as the start of a paragraph, which the current line then continues.
        nonlocal unit_line
        if pending:
            unit_line = pending[0][0]
            unit.extend(text for _, text, _ in pending)
            pending.clear()

    # A sectionized draft starts with '---' and its first section, the YAML front matter,
    # runs to the next section line. Any other file is plain kramdown and is read whole.
    sectionized = bool(lines) and lines[0].startswith("---")
    in_front_matter = sectionized
    rendered = not sectionized
    sections: list[str] = []  # kramdown-rfc's stack of section names
    item_indent = 0  # text column of the latest list item, for its fences and code
    fence_base = 0
    boundary = True  # whether the next line starts at a kramdown block boundary
    for number, raw in enumerate(lines, start=1):
        if ial_bodies and not is_block_ial(QUOTE_RE.sub("", raw.expandtabs(4))):
            apply_ials()  # the run of IAL lines ended: every other line is handled below
        if sectionized:
            if number == 1:
                continue
            marker = SECTION_RE.match(raw)
            if marker:
                # kramdown-rfc cuts the draft here before parsing any Markdown, so no
                # paragraph, fence, comment, table or list carries across this line, and
                # the section's end is a block boundary for a table that ends it.
                commit_table()
                flush()
                fence, comment, item_indent, boundary = None, None, 0, True
                in_front_matter = False
                if marker.group("name"):
                    sections.append(marker.group("name"))
                elif sections:
                    sections.pop()
                rendered = bool(sections) and bool(RENDERED_SECTION_RE.match(sections[-1]))
                continue
            if in_front_matter or not rendered:
                continue
        at_boundary, boundary = boundary, False
        # Tabs stop every 4 columns as kramdown counts them; all blockquote markers (nested
        # quotes included) are removed before any boundary is decided, so a bare '>'
        # separates quoted paragraphs.
        line = QUOTE_RE.sub("", raw.expandtabs(4))
        stripped = line.strip()
        column = len(line) - len(line.lstrip())
        fence_match = FENCE_RE.match(line)
        if fence:
            if (
                fence_match
                and len(fence_match.group(1)) <= fence_base + 3
                and len(fence_match.group(2)) >= len(fence)
                and not fence_match.group(3).strip()
            ):
                fence = None
            continue
        if pending and comment is None:
            # Decide the pending table first: kramdown checks for its end before anything
            # else on this line. A new list item ends a table inside the previous item.
            ends_item = pending_in_item and LIST_ITEM_RE.match(line)
            if not stripped or IAL_LINE_RE.match(line) or ends_item:
                commit_table()
            elif is_table_line(stripped):
                # Read the row's visible text only, as for the table's first row: a
                # keyword inside an HTML or {::comment} comment is no rule, whether the
                # rows end up a table or a paragraph. A comment the row leaves open
                # carries on to the next lines.
                visible, comment, _ = strip_comments(line, None)
                pending.append((number, visible.strip(), visible.strip()))
                continue
            else:
                revert_table()
        if comment is None:
            if not unit and stripped and column < item_indent and not LIST_ITEM_RE.match(line):
                item_indent = 0  # a line left of the item's text ends its list
            if fence_match and len(fence_match.group(1)) <= item_indent + 3:
                flush()
                fence, fence_base = fence_match.group(2), item_indent
                continue
            if stripped and not unit and column >= item_indent + 4:
                continue  # indented code block: kramdown renders it as code, not prose
        visible, comment, had_comment = strip_comments(line, comment)
        if had_comment:
            if not visible.strip():
                continue  # only comment text: no prose, and no paragraph break either
            line = visible
            stripped = line.strip()
            # Text right after a comment is no block boundary for a table still pending.
            revert_table()
        # A '{:...}' line indented 4 or more columns past the open list item's text, inside
        # a paragraph, is no definition or IAL: kramdown reads it as more paragraph text
        # (ALD_START and IAL_BLOCK_START allow at most 3 leading spaces). With no
        # paragraph open, such a line is an indented code block and was skipped above.
        indented = bool(unit) and column >= item_indent + 4
        ald = None if indented else ALD_DEF_RE.match(line)
        if ald:
            # A definition, not an IAL: kramdown reads it before trying a block IAL, so it
            # tags nothing, yet it ends the paragraph before it. Code and comments were
            # skipped above, so a definition inside them is never recorded.
            found_alds.setdefault(ald.group("name"), []).append(ald.group("attrs"))
            flush()
            boundary = True
            continue
        ial = None if indented else IAL_LINE_RE.match(line)
        if ial:
            boundary = True
            if ial_bodies:
                ial_bodies.append(ial.group("body"))  # the next IAL line on the same block
                continue
            if unit and item_indent and column < item_indent and LIST_ITEM_RE.match(unit[0]):
                # Left of the item's text, kramdown ends the list here and puts the IAL on
                # the whole list, not the item (checked with kramdown-rfc), so the item's
                # rule gets no ID from it. flush still warns when the item has its own.
                req_id = ial_req_id(ial.group("body"), known_alds)
                marker = LIST_ITEM_RE.match(unit[0])
                if marker and LEADING_IAL_RE.match(unit[0], marker.end()):
                    flush(req_id)  # flush keeps the item's leading ID and warns about this one
                    continue
                if req_id:
                    warnings.append(
                        f"{relative}:{number}: {req_id} is not indented to its list item's text, "
                        "so kramdown-rfc puts it on the whole list"
                    )
                flush()
                continue
            if unit and is_block_ial(line):
                ial_bodies.append(ial.group("body"))  # applied once the run of IALs ends
                continue
            req_id = ial_req_id(ial.group("body"), known_alds)
            if req_id and not unit:
                warnings.append(
                    f"{relative}:{number}: {req_id} is not on the line right after a rule, so it tags nothing"
                )
            flush(req_id)
            continue
        if not stripped:
            flush()
            boundary = True
            continue
        if len(unit) == 1 and unit_line == number - 1 and not item_indent and SETEXT_UNDERLINE_RE.match(line):
            # 'Title\n=====': the line above is a setext heading, not a rule (checked with
            # kramdown-rfc). After two or more lines of text the underline is just more of
            # the paragraph, as kramdown only reads a setext heading at a block's start.
            heading = parse_heading("# " + unit[0])
            unit.clear()
            section = (heading[1] or slugify(heading[0])) if heading else section
            continue
        heading = parse_heading(line)
        if heading:
            flush()
            item_indent = 0
            section = heading[1] or slugify(heading[0])
            continue
        item = LIST_ITEM_RE.match(line)
        if item and unit and not item_indent:
            # A bullet or number can't interrupt a top-level paragraph: kramdown reads
            # '402. more' or '- more' after a line of prose as more of that paragraph
            # (checked with kramdown-rfc). Inside a list item it starts the next item.
            item = None
        item = item or DEFINITION_RE.match(line)
        if item:
            flush()
            item_indent = item.end()
        if not unit:
            # A list item's text starts its own block boundary; other text needs one.
            row = line[item.end() :] if item else stripped
            if (item or at_boundary) and is_table_line(row):
                pending.append((number, stripped, row.strip()))
                pending_in_item = bool(item)
                continue
            unit_line = number
        unit.append(stripped)
    apply_ials()
    commit_table()
    flush()
    if alds is None and found_alds:
        # Some IAL may name a definition that only comes later: read the draft again
        # with every definition known. The second reading finds the same ones, since
        # which lines are code or comments does not depend on IDs.
        return extract_requirements(path, relative, found_alds)

    seen: dict[str, int] = {}
    for requirement in requirements:
        count = seen.get(requirement.fallback_key, 0) + 1
        seen[requirement.fallback_key] = count
        if count > 1:
            requirement.fallback_key = f"{requirement.fallback_key}-{count}"
        if requirement.req_id and not requirement.req_id.startswith(f"req-{draft}-"):
            requirement.id_problem = requirement.id_problem or "foreign"
            warnings.append(
                f"{relative}:{requirement.line}: {requirement.req_id} does not use this draft's prefix req-{draft}-; not counted as covered"
            )
    return requirements, warnings, table_rows


def draft_paths(specs_dir: Path) -> list[Path]:
    """Return the drafts under specs/, or fail when specs_dir is not an mpp-specs checkout."""
    if not specs_dir.is_dir():
        raise ValueError(f"specs path {specs_dir} does not exist or is not a directory")
    root = (specs_dir / "specs").resolve()
    # A symlinked draft could point anywhere; its text would be copied into the report.
    paths = sorted(
        p for p in (specs_dir / "specs").rglob("*.md")
        if not p.is_symlink() and p.resolve().is_relative_to(root)
    )
    # Every specs/**/*.md is read as a draft, as mpp-specs' gen.sh and linter do, but a
    # tree with no draft-*.md at all (only a README, say) is not an mpp-specs checkout.
    if not any(p.name.startswith("draft-") for p in paths):
        raise ValueError(
            f"{specs_dir} has no specs/**/draft-*.md drafts; pass an mpp-specs checkout"
        )
    return paths


def extract_all(
    specs_dir: Path,
) -> tuple[list[Requirement], list[str], dict[str, int], dict[str, str]]:
    """Extract requirements from every draft under specs/, flagging duplicate IDs.

    Returns the requirements, the warnings, each draft's keyword table rows and each
    draft's file. The last two list every draft read, including one with no prose rules
    (such as a draft whose keywords are all in tables), so the report still shows it.
    """
    requirements: list[Requirement] = []
    warnings: list[str] = []
    table_rows: dict[str, int] = {}
    first_path: dict[str, str] = {}
    # Highest revision first, so a stray older copy of a draft is the one skipped.
    for path in sorted(draft_paths(specs_dir), key=lambda p: p.name, reverse=True):
        relative = path.relative_to(specs_dir).as_posix()
        short = draft_short_name(path.name)
        if short in first_path:
            # Two files with one short name would merge their rules into one draft.
            warnings.append(
                f"{relative}: same draft name '{short}' as {first_path[short]}; skipped"
            )
            continue
        first_path[short] = relative
        found, found_warnings, rows = extract_requirements(path, relative)
        requirements.extend(found)
        warnings.extend(found_warnings)
        table_rows[short] = rows
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
    # Flow cases only: server_only, raw_http, client_flow or ordinary, as flow_runner runs them.
    flow_mode: str | None = None
    # Why no SDK can pass this claim for real, if none can: the vector runner refuses the
    # claim's whole file, or the scenario calls a legacy command that maps to no adapter
    # operation (the runner fails it before starting any SDK, so it could only "pass" by
    # expecting that failure); or a flow case's challenge_path names no case's path.
    runner_error: str | None = None
    # Vector claims only: further operations the scenario's tests call (a parse and format
    # scenario needs both), each of which the adapter must advertise.
    operations: tuple[str, ...] = ()
    # Vector claims only: per adapter, the sdkVersions expressions of every scenario in this
    # claim's file that selects the adapter (this scenario included), in file order. As the
    # runner walks the file for an adapter it resolves the adapter's SDK version and parses
    # each such expression; if either raises, the whole file is aborted for that adapter.
    file_constraints: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
        for scenario in vector["scenarios"]:
            try:
                check_scenario(runner, scenario)
            except ValueError as error:
                file_error = f"the runner refuses its file (vectors/{path.name}#{scenario['name']}: {error})"
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
                )
            )
    flow_cases = load_flow_cases(flow_cases_path)
    # flow_runner looks a challenge_path up among every case's path, server_only ones
    # included, and fails the case before sending anything when none has it.
    # Last case wins for a shared path, as in flow_runner's cases_by_path.
    cases_by_path = {str(flow_case.get("path", "/")): flow_case for flow_case in flow_cases}
    flow_paths = set(cases_by_path)
    for flow_case in flow_cases:
        if flow_case.get("requirements"):
            challenge_path = flow_case.get("challenge_path")
            flow_error = None
            if challenge_path and str(challenge_path) not in flow_paths:
                flow_error = (
                    f"challenge_path {challenge_path} names no flow case's path, "
                    "so flow_runner fails it before any request"
                )
            claims.append(
                Claim(
                    source=f"flows/{flow_cases_path.name}#{flow_case['name']}",
                    requirements=flow_case["requirements"],
                    flow_mode=flow_mode(flow_case, cases_by_path),
                    runner_error=flow_error,
                )
            )
    return claims


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
    if flow_case.get("discovery") or flow_case.get("json_rpc"):
        return "raw_http"
    if flow_case.get("client_flow"):
        return "client_flow"
    if flow_case.get("no_payment"):
        return "raw_http"
    # The first request goes to challenge_path's case when one is named. The compliance
    # server answers that case without a challenge when it is a force_status (any status,
    # 402 included, with no WWW-Authenticate header), no_payment (200), discovery or
    # json_rpc (both 200) case, so flow_runner stops (unexpected_status or
    # missing_challenge) before it calls the adapter: it checks the reference server,
    # not an SDK.
    first = flow_case
    if flow_case.get("challenge_path") and cases_by_path is not None:
        first = cases_by_path.get(str(flow_case["challenge_path"]), flow_case)
    if any(first.get(flag) for flag in NO_CHALLENGE_FLAGS):
        return "raw_http"
    return "ordinary"


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
        if claim.flow_mode in ("server_only", "raw_http"):
            return False
        return (
            claim.flow_mode != "client_flow" or "http.payment_request" in capabilities
        )
    if claim.adapters is not None and adapter not in claim.adapters:
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
        # nothing either. Other flow cases run against the reference server; raw_http ones
        # need no adapter, so they count suite-wide as they are.
        if claim.flow_mode == "server_only":
            message = f"{claim.source}: server_only flow cases are not run; not counted as coverage"
            if message not in warnings:
                warnings.append(message)
            continue
        if claim.flow_mode is None and not any(
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
        "coverage": {
            req_id: sorted(sources) for req_id, sources in sorted(coverage.items())
        },
        "unknown_references": unknown,
        # Every claim citing each unknown ID, including claims that cover nothing.
        "unknown_reference_sources": {
            req_id: sorted(set(cited[req_id])) for req_id in unknown
        },
        "profiles": profile_report,
        "warnings": warnings,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report as a Markdown summary with the uncovered rules per draft."""
    specs = report["specs"]
    totals = report["totals"]
    lines = [
        "# MPP requirement gap report",
        "",
        f"Specs: {specs.get('source')} at {specs.get('commit') or 'unknown commit'}.",
        "",
        f"{totals['rules']} rules, {totals['tagged']} tagged with an ID, {totals['covered']} covered by a vector or flow.",
        f"{totals['table_rows']} table rows also carry a keyword; they are counted, not extracted.",
        "",
        "| Draft | Rules | Tagged | Covered | Table rows |",
        "|-------|------:|-------:|--------:|-----------:|",
    ]
    for entry in report["drafts"]:
        lines.append(
            f"| {entry['draft']} | {entry['rules']} | {entry['tagged']} | {entry['covered']} | {entry['table_rows']} |"
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
                    f"| {adapter} | {draft} | {counts['rules']} | {counts['covered']} |"
                )
    if report["unknown_references"]:
        lines += ["", "## Unknown requirement IDs", ""]
        lines += [
            f"- \x60{req_id}\x60: {', '.join(report['unknown_reference_sources'][req_id])}"
            for req_id in report["unknown_references"]
        ]
    if report["warnings"]:
        lines += ["", "## Warnings", ""]
        lines += [f"- {warning}" for warning in report["warnings"]]
    lines += ["", "## Uncovered rules"]
    for entry in report["drafts"]:
        if not entry["uncovered"]:
            continue
        lines += ["", f"### {entry['draft']} ({entry['file']})", ""]
        for rule in entry["uncovered"]:
            text = rule["excerpt"].replace("|", "\\|")
            problem = f" [{rule['id_problem']} ID, not credited]" if rule.get("id_problem") else ""
            lines.append(f"- \x60{rule['key']}\x60 (line {rule['line']}){problem}: {text}")
    return "\n".join(lines) + "\n"


def git_blob_sha(data: bytes) -> str:
    """Return the git blob id of data, as 'git hash-object' computes it without filters."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_commit(path: Path) -> str | None:
    """Return the commit the specs at path were read from, or None if unknown.

    None when path is not the root of a git checkout (its HEAD would belong to an
    enclosing repository). A "-dirty" suffix when the drafts the report reads
    differ from HEAD's: the specs/**/*.md files draft discovery finds in the
    working tree are compared, by path and blob id, with the ones in HEAD's tree.
    Comparing the files themselves rather than parsing 'git status' also covers
    untracked, gitignored, oddly named and nested-repository files.

    Symlinked drafts are left out on both sides, as draft discovery skips them. A file
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
        for entry in git("ls-tree", "-r", "-z", "HEAD", "--", "specs").split("\0"):
            if not entry:
                continue
            meta, name = entry.split("\t", 1)
            mode, kind, sha = meta.split()
            if name.endswith(".md") and kind == "blob" and mode != "120000":
                head[name] = sha
        working: dict[str, str] = {}
        for file in (path / "specs").rglob("*.md"):
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


def generate(specs_dir: Path, source: str) -> dict[str, Any]:
    """Build the report for one mpp-specs checkout against this suite's fixtures."""
    # Sample the checkout's commit and the drafts' exact contents before and after
    # reading them: if either changed in between, the rules counted may not match the
    # commit named, so refuse to name one rather than attest the wrong source. The
    # fingerprint catches a draft that changes again in an already dirty checkout, whose
    # label stays '<HEAD>-dirty'.
    fingerprint_before = specs_fingerprint(specs_dir)
    commit_before = git_commit(specs_dir)
    requirements, warnings, table_rows, draft_files = extract_all(specs_dir)
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
    args = parser.parse_args(argv)

    try:
        if args.specs:
            report = generate(args.specs.resolve(), str(args.specs))
        else:
            with tempfile.TemporaryDirectory(prefix="mpp-specs-") as directory:
                specs_dir = clone_specs(Path(directory) / "mpp-specs", args.ref)
                report = generate(specs_dir, f"{SPECS_REPOSITORY}@{args.ref}")
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
