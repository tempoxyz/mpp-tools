#!/usr/bin/env python3
"""
Grammar-based differential fuzzing driven by the specification's ABNF.

Extracts the ABNF blocks from the core mpp-specs draft
(specs/core/draft-httpauth-payment-01.md, including its "ABNF Collected"
appendix), completes them with the RFC 9110 rules they reference (token,
quoted-string, quoted-pair, OWS, BWS, weight, ...) and the RFC 5234 core rules
(ALPHA, DIGIT, SP, ...), and generates header values for WWW-Authenticate
challenges, Authorization credentials, Payment-Receipt and Accept-Payment:
values the grammar accepts, and near misses it rejects (method identifiers with
uppercase letters, digits or ':', bad quoting and escapes, bad base64url,
whitespace variants, duplicate parameters).

The verdict for each case comes from parsing it with the grammar, plus the few
rules the draft states in prose (each parameter at most once, the required
parameters, a non-empty id, base64url JSON documents), never from how the case
was generated. Accepted challenges also carry the parameter values the grammar
parses them to: quoted-pair drops the backslash and decodes nothing else.

Each case goes to every adapter through the harness. The report lists adapters
whose accept or reject verdict, or parsed value, disagrees with the grammar, and
adapters that disagree with the TypeScript reference (mppx) in ways
fuzz/allowlist.json does not accept. Accept-Payment has no adapter operation, so
its cases only check the grammar against itself and are counted.

Runs are deterministic for a given --seed, --cases budget and specs checkout.

Usage:
    uv run --locked python scripts/abnf_fuzz.py [--specs PATH] [--adapters python,go]
        [--seed N] [--cases N] [--kinds KINDS] [--report PATH] [--summary PATH] [--report-only]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abnf.grammars import rfc3339, rfc9110
from abnf.grammars.misc import load_grammar_rulelist
from abnf.parser import (
    Alternation,
    Concatenation,
    Literal,
    Node,
    Option,
    ParseError,
    Repetition,
)
from abnf.parser import (
    Rule as AbnfRule,
)
from diff_fuzz import (
    ALLOWLIST_PATH,
    CALL_TIMEOUT,
    REFERENCE,
    AllowlistEntry,
    Divergence,
    classify,
    error_message,
    find_allowlisted,
    load_allowlist,
    outcome,
)
from harness import AdapterClient, AdapterConfig, AdapterSession, discover_adapters

SPECS_REPOSITORY = "https://github.com/tempoxyz/mpp-specs"
CORE_DRAFT = "specs/core/draft-httpauth-payment-01.md"
# Rules the draft references from RFC 9110 (which collects RFC 7235's
# authentication rules) without defining them. RFC 5234 core rules (ALPHA,
# DIGIT, SP, ...) come with every abnf grammar.
RFC9110_RULES = ("token", "quoted-string", "OWS", "BWS", "RWS", "weight", "qvalue")
KINDS = ("challenge", "credential", "receipt", "accept-payment")
REQUIRED_CHALLENGE_PARAMS = ("id", "realm", "method", "intent", "request")
# Parameters a parsed challenge is compared on.
COMPARED_CHALLENGE_FIELDS = ("id", "realm", "method", "intent", "request", "description", "expires")
VALID_SHARE = 0.4

ABNF_FENCE = re.compile(r"^(?P<fence>~~~|```)[ \t]*abnf[ \t]*\n(?P<body>.*?)^(?P=fence)[ \t]*$", re.MULTILINE | re.DOTALL)
RULE_START = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9-]*)\s*(?P<op>=/|=)(?P<rest>.*)$")
LIST_RULE = re.compile(r"(?<![%\w\"-])(?P<min>\d*)#(?P<max>\d*)(?P<element>[A-Za-z][A-Za-z0-9-]*)")


# ---------------------------------------------------------------------------
# Grammar extraction
# ---------------------------------------------------------------------------


def extract_abnf_blocks(markdown: str) -> list[str]:
    """Return the bodies of a draft's abnf code blocks (~~~abnf or ```abnf), in order."""
    return [match.group("body") for match in ABNF_FENCE.finditer(markdown)]


def strip_comment(line: str) -> str:
    """Drop an ABNF comment (';' to end of line) outside quoted literals."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            return line[:index]
    return line


def expand_lists(definition: str) -> str:
    """Rewrite RFC 9110 list rules (#element, 1#element) as plain RFC 5234 ABNF.

    RFC 9110 Section 5.6.1.1: #element is [ element *( OWS "," OWS element ) ]
    and 1#element is element *( OWS "," OWS element ).
    """

    def replace(match: re.Match[str]) -> str:
        low, high, element = match.group("min"), match.group("max"), match.group("element")
        if high or low not in ("", "0", "1"):
            raise ValueError(f"unsupported list rule {match.group(0)!r}")
        listed = f'{element} *( OWS "," OWS {element} )'
        return f"( {listed} )" if low == "1" else f"[ {listed} ]"

    return LIST_RULE.sub(replace, definition)


def parse_rules(blocks: list[str]) -> dict[str, tuple[str, str]]:
    """Collect rule definitions from ABNF blocks, keyed by lowercased name.

    A rule defined again with the same text (the appendix repeats rules from
    the body) is kept once; a different definition is an error, as is '=/'.
    """
    rules: dict[str, tuple[str, str]] = {}
    for block in blocks:
        current: list[str] | None = None
        pieces: list[tuple[str, list[str]]] = []
        for raw in block.splitlines():
            line = strip_comment(raw).rstrip()
            if not line.strip():
                continue
            start = RULE_START.match(line)
            if start and not raw[:1].isspace():
                if start.group("op") == "=/":
                    raise ValueError(f"incremental alternative for {start.group('name')} is not supported")
                current = [start.group("rest").strip()]
                pieces.append((start.group("name"), current))
            elif current is not None:
                current.append(line.strip())
            else:
                raise ValueError(f"ABNF continuation line without a rule: {raw!r}")
        for name, parts in pieces:
            definition = " ".join(part for part in parts if part)
            key = name.lower()
            if key in rules and " ".join(rules[key][1].split()) != " ".join(definition.split()):
                raise ValueError(f"rule {name} is defined twice with different definitions")
            rules[key] = (name, definition)
    return rules


def to_octets(text: str) -> str:
    """Header text as the octets the grammar matches: its UTF-8 bytes, one character each."""
    return text.encode("utf-8", "surrogatepass").decode("latin-1")


def from_octets(text: str) -> str:
    return text.encode("latin-1").decode("utf-8", "replace")


class Grammar:
    """The draft's ABNF as abnf parsers, with the RFC 9110 and RFC 5234 rules it uses."""

    def __init__(self, rules: dict[str, tuple[str, str]]):
        if not rules:
            raise ValueError("no ABNF rules found")
        self.rules = rules
        text = "\n".join(f"{name} = {expand_lists(definition)}" for name, definition in rules.values()) + "\n"
        imports = [(name, rfc9110.Rule(name)) for name in RFC9110_RULES if name.lower() not in rules]
        rule_class = type("SpecRule", (AbnfRule,), {"grammar": text, "__doc__": "Rules from the MPP draft."})
        self.rule_class: type[AbnfRule] = load_grammar_rulelist(imports)(rule_class)

    @classmethod
    def from_markdown(cls, *markdowns: str) -> Grammar:
        blocks = [block for markdown in markdowns for block in extract_abnf_blocks(markdown)]
        return cls(parse_rules(blocks))

    @classmethod
    def from_specs(cls, specs_dir: Path) -> Grammar:
        draft = specs_dir / CORE_DRAFT
        if not draft.is_file():
            raise ValueError(f"{specs_dir} has no {CORE_DRAFT}; pass an mpp-specs checkout")
        return cls.from_markdown(draft.read_text(encoding="utf-8"))

    def rule(self, name: str) -> AbnfRule:
        return self.rule_class(name)

    def parse(self, name: str, text: str) -> Node | None:
        """Parse text as the named rule, or None when the grammar rejects it.

        Header values are octets, so a non-ASCII character is matched as its
        UTF-8 bytes: obs-text inside a quoted-string and nowhere else.
        """
        try:
            return self.rule(name).parse_all(to_octets(text))
        except ParseError:
            return None

    def accepts(self, name: str, text: str) -> bool:
        return self.parse(name, text) is not None


# ---------------------------------------------------------------------------
# Generation from the grammar
# ---------------------------------------------------------------------------


class _Unusable(Exception):
    """An alternative that cannot produce text under the generator's limits."""


class Generator:
    """Produce random strings that a grammar rule matches.

    Repetitions stay short, and past a depth the generator stops taking
    optional parts, so recursion ends. ascii_only keeps obs-text (%x80-FF) out.
    """

    def __init__(self, grammar: Grammar, rng: random.Random, *, ascii_only: bool = True, max_extra: int = 6):
        self.grammar = grammar
        self.rng = rng
        self.ascii_only = ascii_only
        self.max_extra = max_extra

    def generate(self, name: str, *, min_length: int = 0, max_length: int = 64) -> str:
        for _ in range(64):
            value = self._gen(self.grammar.rule(name), 0)
            if min_length <= len(value) <= max_length:
                return value
        raise ValueError(f"could not generate {name} between {min_length} and {max_length} characters")

    def _gen(self, parser: Any, depth: int) -> str:
        deep = depth > 24
        if isinstance(parser, AbnfRule):
            return self._gen(parser.definition, depth + 1)
        if isinstance(parser, Literal):
            value = parser.value
            if isinstance(value, tuple):
                low, high = ord(value[0]), ord(value[1])
                if self.ascii_only:
                    high = min(high, 0x7E)
                if low > high:
                    raise _Unusable()
                return chr(self.rng.randint(low, high))
            return value
        if isinstance(parser, Concatenation):
            return "".join(self._gen(part, depth + 1) for part in parser.parsers)
        if isinstance(parser, Alternation):
            options = list(parser.parsers)
            self.rng.shuffle(options)
            for option in options:
                try:
                    return self._gen(option, depth + 1)
                except _Unusable:
                    continue
            raise _Unusable()
        if isinstance(parser, Option):
            if deep or self.rng.random() < 0.5:
                return ""
            return self._gen(parser.alternation, depth + 1)
        if isinstance(parser, Repetition):
            low, high = parser.repeat.min, parser.repeat.max
            extra = 0 if deep else min(self.rng.randint(0, self.max_extra), self.rng.randint(0, self.max_extra))
            count = low + extra if high is None else min(high, low + extra)
            return "".join(self._gen(parser.element, depth + 1) for _ in range(count))
        raise TypeError(f"unsupported ABNF parser {type(parser).__name__}")


# ---------------------------------------------------------------------------
# Oracle: the grammar's verdict, plus the draft's prose rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    accept: bool
    reason: str
    expected: dict[str, Any] | None = None


def _children(node: Node) -> list[Node]:
    return list(getattr(node, "children", None) or [])


def find_nodes(node: Node, name: str) -> list[Node]:
    """The outermost nodes of a rule in a parse tree, in order."""
    if node.name.lower() == name.lower():
        return [node]
    found: list[Node] = []
    for child in _children(node):
        found.extend(find_nodes(child, name))
    return found


def unquote(node: Node) -> str:
    """The value of a quoted-string node: qdtext as is, quoted-pair without its backslash."""
    out: list[str] = []
    for child in _children(node):
        if child.name == "qdtext":
            out.append(child.value)
        elif child.name == "quoted-pair":
            out.append(child.value[1:])
    return from_octets("".join(out))


def auth_params(node: Node) -> list[tuple[str, str]]:
    """The (name, value) pairs of every auth-param in a parse tree, in order."""
    params: list[tuple[str, str]] = []
    for param in find_nodes(node, "auth-param"):
        name, value = [child for child in _children(param) if child.name in ("token", "quoted-string")][:2]
        params.append((name.value, unquote(value) if value.name == "quoted-string" else value.value))
    return params


def b64url(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_base64url(text: str) -> bytes | None:
    """Strictly decode unpadded base64url, or None (wrong alphabet, length or trailing bits)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if b64url(data) == text else None


def decode_json_object(text: str) -> dict[str, Any] | None:
    data = decode_base64url(text)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _method_reason(grammar: Grammar, method: Any, where: str) -> str | None:
    if not isinstance(method, str) or not grammar.accepts("payment-method-id", method):
        return f"{where} does not match payment-method-id"
    return None


def challenge_params_verdict(grammar: Grammar, params: list[tuple[str, str]]) -> Verdict:
    """Apply the prose rules to a grammatical challenge's auth-params."""
    seen: set[str] = set()
    for name, _ in params:
        if name.lower() in seen:
            return Verdict(False, f"parameter {name.lower()} repeated (RFC 9110 Section 11.2)")
        seen.add(name.lower())
    values = {name.lower(): value for name, value in params}
    for name in REQUIRED_CHALLENGE_PARAMS:
        if name not in values:
            return Verdict(False, f"required parameter {name} missing")
    if not values["id"]:
        return Verdict(False, "id is empty")
    reason = _method_reason(grammar, values["method"], "method")
    if reason:
        return Verdict(False, reason)
    if not grammar.accepts("intent-token", values["intent"]):
        return Verdict(False, "intent does not match intent-token")
    request = decode_json_object(values["request"])
    if request is None:
        return Verdict(False, "request is not a base64url-nopad JSON object")
    expected: dict[str, Any] = {key: values[key] for key in COMPARED_CHALLENGE_FIELDS if key in values}
    expected["request"] = request
    return Verdict(True, "accepted", expected)


def challenge_verdict(grammar: Grammar, header: str) -> Verdict:
    tree = grammar.parse("payment-challenge", header)
    if tree is None:
        return Verdict(False, "grammar rejects payment-challenge")
    return challenge_params_verdict(grammar, auth_params(tree))


def _echo_reason(grammar: Grammar, challenge: Any) -> str | None:
    if not isinstance(challenge, dict):
        return "challenge is not an object"
    for name in REQUIRED_CHALLENGE_PARAMS:
        if not isinstance(challenge.get(name), str):
            return f"challenge.{name} missing"
    if not challenge["id"]:
        return "challenge.id is empty"
    reason = _method_reason(grammar, challenge["method"], "challenge.method")
    if reason:
        return reason
    if not grammar.accepts("intent-token", challenge["intent"]):
        return "challenge.intent does not match intent-token"
    if decode_json_object(challenge["request"]) is None:
        return "challenge.request is not a base64url-nopad JSON object"
    return None


def credential_verdict(grammar: Grammar, header: str, *, with_request: bool = False) -> Verdict:
    """Whether a Credential header is valid, and what it parses to.

    with_request adds the decoded challenge.request to the expected values, for
    checking a formatted Credential against the document it was formatted from.
    """
    tree = grammar.parse("payment-credentials", header)
    if tree is None:
        return Verdict(False, "grammar rejects payment-credentials")
    document = decode_json_object(from_octets(find_nodes(tree, "base64url-nopad")[0].value))
    if document is None:
        return Verdict(False, "credential is not a base64url-nopad JSON object")
    reason = _echo_reason(grammar, document.get("challenge"))
    if reason:
        return Verdict(False, reason)
    if not isinstance(document.get("payload"), dict):
        return Verdict(False, "payload is not an object")
    challenge = document["challenge"]
    expected = {
        "challenge": {key: challenge[key] for key in ("id", "realm", "method", "intent")},
        "payload": document["payload"],
    }
    if with_request:
        expected["challenge"]["request"] = decode_json_object(challenge["request"])
    return Verdict(True, "accepted", expected)


def receipt_verdict(grammar: Grammar, header: str) -> Verdict:
    if not grammar.accepts("Payment-Receipt", header):
        return Verdict(False, "grammar rejects Payment-Receipt")
    document = decode_json_object(header)
    if document is None:
        return Verdict(False, "receipt is not a base64url-nopad JSON object")
    for key in ("status", "method", "timestamp", "reference"):
        if not isinstance(document.get(key), str):
            return Verdict(False, f"receipt {key} missing")
    reason = _method_reason(grammar, document["method"], "receipt method")
    if reason:
        return Verdict(False, reason)
    try:
        rfc3339.Rule("date-time").parse_all(document["timestamp"])
    except ParseError:
        return Verdict(False, "receipt timestamp is not an RFC 3339 date-time")
    expected = {key: document[key] for key in ("status", "method", "reference") if key in document}
    return Verdict(True, "accepted", expected)


def challenge_object_verdict(grammar: Grammar, challenge: dict[str, Any]) -> Verdict:
    """Whether formatting a Challenge must succeed: its method and intent must fit the grammar."""
    reason = _method_reason(grammar, challenge.get("method"), "method")
    if reason:
        return Verdict(False, reason)
    if not isinstance(challenge.get("intent"), str) or not grammar.accepts("intent-token", challenge["intent"]):
        return Verdict(False, "intent does not match intent-token")
    expected = {key: challenge[key] for key in COMPARED_CHALLENGE_FIELDS if key in challenge}
    return Verdict(True, "accepted", expected)


def accept_payment_verdict(grammar: Grammar, header: str) -> Verdict:
    if grammar.accepts("Accept-Payment", header):
        return Verdict(True, "accepted")
    return Verdict(False, "grammar rejects Accept-Payment")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@dataclass
class Case:
    kind: str
    operation: str | None
    mutation: str
    input: Any
    verdict: Verdict


REALMS = ("api.example.com", "merchant.example", "Example API", "pay.example.com:8443", "a")
DESCRIPTIONS = ("Premium API access", "One-time charge, 10 credits", "price: $0.01 / call", "x")
CHALLENGE_MUTATIONS = (
    "scheme-case-and-spaces", "token-values", "mixed-whitespace",
    "method-uppercase", "method-digit", "method-colon", "method-hyphen-underscore", "method-empty",
    "quoted-pair-unicode-escape", "quoted-pair-any-char", "unterminated-quote", "unescaped-dquote",
    "trailing-backslash", "ctl-in-quoted-string", "request-padding", "request-standard-alphabet",
    "request-invalid-char", "scheme-tab", "scheme-no-space", "bws-around-equals", "ows-around-comma",
    "missing-comma", "trailing-comma", "empty-list-element", "duplicate-param", "duplicate-param-case",
    "missing-required", "empty-id",
)
METHOD_MUTATIONS = ("method-uppercase", "method-digit", "method-colon", "method-hyphen-underscore", "method-empty")
TOKEN_MUTATIONS = ("token-padding", "token-standard-alphabet", "token-invalid-char")
CREDENTIAL_MUTATIONS = (
    "method-uppercase", "method-digit", "method-colon", *TOKEN_MUTATIONS,
    "scheme-tab", "scheme-no-space", "trailing-text", "quoted-token", "not-json",
)
RECEIPT_MUTATIONS = ("method-uppercase", "method-digit", "method-colon", *TOKEN_MUTATIONS, "inner-space", "not-json")
ACCEPT_PAYMENT_MUTATIONS = ("method-uppercase", "method-digit", "method-colon", "missing-slash", "bad-weight", "trailing-comma")


class CaseFactory:
    """Build challenge, credential, receipt and Accept-Payment cases from the grammar."""

    def __init__(self, grammar: Grammar, rng: random.Random):
        self.grammar = grammar
        self.rng = rng
        self.gen = Generator(grammar, rng)

    # -- values ------------------------------------------------------------

    def method(self) -> str:
        return self.gen.generate("payment-method-id", min_length=2, max_length=10)

    def intent(self) -> str:
        return self.rng.choice(["charge", "session", self.gen.generate("intent-token", min_length=1, max_length=12)])

    def token(self) -> str:
        return self.gen.generate("token", min_length=4, max_length=16)

    def ows(self) -> str:
        return self.gen.generate("OWS", max_length=3)

    def request(self) -> dict[str, Any]:
        document: dict[str, Any] = {"amount": str(self.rng.randint(1, 10**9)), "currency": "usd"}
        if self.rng.random() < 0.5:
            document["recipient"] = "0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(40))
        if self.rng.random() < 0.3:
            document["memo"] = self.rng.choice(["inv_123", "a/b+c", "~?>"])
        return document

    def challenge_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "id": self.token(),
            "realm": self.rng.choice(REALMS),
            "method": self.method(),
            "intent": self.intent(),
            "request": self.request(),
        }
        if self.rng.random() < 0.3:
            fields["description"] = self.rng.choice(DESCRIPTIONS)
        if self.rng.random() < 0.2:
            fields["expires"] = "2030-01-01T00:00:00Z"
        return fields

    def mutate_method(self, method: str, mutation: str) -> str:
        if mutation == "method-uppercase":
            return self.rng.choice([str.capitalize, str.upper])(method)
        if mutation == "method-digit":
            return method + str(self.rng.randint(0, 9))
        if mutation == "method-colon":
            return method + ":" + self.method()
        if mutation == "method-hyphen-underscore":
            return method + self.rng.choice("-_") + self.method()
        if mutation == "method-empty":
            return ""
        raise ValueError(f"unknown method mutation {mutation}")

    def mutate_token(self, token: str, mutation: str, invalid: str) -> str:
        if mutation == "token-padding":
            return token + "=" * max(1, -len(token) % 4)
        if mutation == "token-standard-alphabet":
            swapped = token.replace("-", "+").replace("_", "/")
            return swapped if swapped != token else token + "+/"
        if mutation == "token-invalid-char":
            at = self.rng.randint(1, len(token) - 1)
            return token[:at] + self.rng.choice(invalid) + token[at:]
        raise ValueError(f"unknown token mutation {mutation}")

    # -- challenges --------------------------------------------------------

    def quote(self, value: str, *, escape_share: float = 0.0) -> str:
        out = []
        for char in value:
            if char in '"\\' or (escape_share and self.rng.random() < escape_share):
                out.append("\\" + char)
            else:
                out.append(char)
        return '"' + "".join(out) + '"'

    def render_value(self, value: str, style: str) -> str:
        if style == "raw":
            return f'"{value}"'
        if style == "token" and value and self.grammar.accepts("token", value):
            return value
        if style == "escaped":
            return self.quote(value, escape_share=0.4)
        return self.quote(value)

    def challenge_case(self, mutation: str) -> Case:
        fields = self.challenge_fields()
        params = [
            (key, b64url(json.dumps(value, separators=(",", ":"))) if key == "request" else str(value))
            for key, value in fields.items()
        ]
        self.rng.shuffle(params)
        styles: dict[str, str] = {}
        scheme = "Payment "
        separator: Callable[[], str] = lambda: ", "
        equals: Callable[[], str] = lambda: "="
        suffix = ""

        def set_param(name: str, value: str, style: str = "quoted") -> None:
            params[:] = [(key, value if key == name else current) for key, current in params]
            styles[name] = style

        if mutation == "valid":
            pass
        elif mutation == "scheme-case-and-spaces":
            scheme = self.rng.choice(["payment ", "PAYMENT ", "pAyMeNt ", "Payment  ", "Payment   "])
        elif mutation == "token-values":
            styles = {name: "token" for name, _ in params}
        elif mutation == "mixed-whitespace":
            separator = lambda: self.ows() + "," + self.ows()
            equals = lambda: self.ows() + "=" + self.ows()
        elif mutation in METHOD_MUTATIONS:
            set_param("method", self.mutate_method(fields["method"], mutation), self.rng.choice(["quoted", "token"]))
        elif mutation == "quoted-pair-unicode-escape":
            set_param("realm", "merchant\\u2014" + fields["realm"], "raw")
        elif mutation == "quoted-pair-any-char":
            styles = {name: "escaped" for name, _ in params}
        elif mutation == "unterminated-quote":
            styles = {name: "quoted" for name, _ in params}
            suffix = "<drop-last>"
        elif mutation == "unescaped-dquote":
            set_param("realm", fields["realm"][:1] + '"' + fields["realm"][1:], "raw")
        elif mutation == "trailing-backslash":
            set_param("realm", fields["realm"] + "\\", "raw")
        elif mutation == "ctl-in-quoted-string":
            set_param("realm", fields["realm"] + self.rng.choice(["\x00", "\x7f", "\n", "\r\n", "\x1b"]) + "x")
        elif mutation == "request-padding":
            set_param("request", self.mutate_token(dict(params)["request"], "token-padding", ""))
        elif mutation == "request-standard-alphabet":
            set_param("request", self.mutate_token(dict(params)["request"], "token-standard-alphabet", ""))
        elif mutation == "request-invalid-char":
            set_param("request", self.mutate_token(dict(params)["request"], "token-invalid-char", ".!~ "))
        elif mutation == "scheme-tab":
            scheme = "Payment\t"
        elif mutation == "scheme-no-space":
            scheme = "Payment"
        elif mutation == "bws-around-equals":
            equals = lambda: self.rng.choice([" ", "\t", "  "]) + "=" + self.rng.choice([" ", "\t", ""])
        elif mutation == "ows-around-comma":
            separator = lambda: self.rng.choice([" ", "\t", "  ", ""]) + "," + self.rng.choice(["\t", "  ", ""])
        elif mutation == "missing-comma":
            separator = lambda: " "
        elif mutation == "trailing-comma":
            suffix = ","
        elif mutation == "empty-list-element":
            separator = lambda: ", , "
        elif mutation == "duplicate-param":
            name, value = self.rng.choice([param for param in params if param[0] != "request"])
            params.append((name, value if self.rng.random() < 0.5 else value + "x"))
        elif mutation == "duplicate-param-case":
            name, value = self.rng.choice(params)
            params.append((name.upper(), value))
        elif mutation == "missing-required":
            drop = self.rng.choice(REQUIRED_CHALLENGE_PARAMS)
            params[:] = [param for param in params if param[0] != drop]
        elif mutation == "empty-id":
            set_param("id", "")
        else:
            raise ValueError(f"unknown challenge mutation {mutation}")

        rendered = [
            f"{name}{equals()}{self.render_value(value, styles.get(name, 'quoted'))}"
            for name, value in params
        ]
        header = scheme + rendered[0]
        for piece in rendered[1:]:
            header += separator() + piece
        header = header[:-1] if suffix == "<drop-last>" else header + suffix
        return Case("challenge", "challenge.parse", mutation, {"header": header}, challenge_verdict(self.grammar, header))

    def challenge_format_case(self, mutation: str) -> Case:
        fields = self.challenge_fields()
        if mutation != "valid":
            fields["method"] = self.mutate_method(fields["method"], mutation)
        return Case("challenge", "challenge.format", mutation, fields, challenge_object_verdict(self.grammar, fields))

    # -- credentials -------------------------------------------------------

    def credential_document(self) -> dict[str, Any]:
        fields = self.challenge_fields()
        fields.pop("description", None)
        echo = {**fields, "request": b64url(json.dumps(fields["request"], separators=(",", ":")))}
        return {"challenge": echo, "payload": {"signature": "0x" + "ab" * self.rng.randint(1, 8)}}

    def credential_case(self, mutation: str) -> Case:
        document = self.credential_document()
        scheme = "Payment "
        suffix = ""
        if mutation == "valid":
            scheme = self.rng.choice(["Payment ", "payment ", "Payment   "])
        elif mutation in METHOD_MUTATIONS:
            document["challenge"]["method"] = self.mutate_method(document["challenge"]["method"], mutation)
        token = b64url(json.dumps(document, separators=(",", ":")))
        if mutation in TOKEN_MUTATIONS:
            token = self.mutate_token(token, mutation, ".!\" ")
        elif mutation == "scheme-tab":
            scheme = "Payment\t"
        elif mutation == "scheme-no-space":
            scheme = "Payment"
        elif mutation == "trailing-text":
            suffix = self.rng.choice([', Basic realm="x"', ",", " x"])
        elif mutation == "quoted-token":
            token = f'"{token}"'
        elif mutation == "not-json":
            token = b64url(self.rng.choice(["hello", "[1,2]", "null", '"credential"']))
        elif mutation != "valid" and mutation not in METHOD_MUTATIONS:
            raise ValueError(f"unknown credential mutation {mutation}")
        header = scheme + token + suffix
        return Case("credential", "credential.parse", mutation, {"header": header}, credential_verdict(self.grammar, header))

    def credential_format_case(self) -> Case:
        document = self.credential_document()
        echo = document["challenge"]
        echo["request"] = json.loads(base64.urlsafe_b64decode(echo["request"] + "=" * (-len(echo["request"]) % 4)))
        expected = {
            "challenge": {key: echo[key] for key in ("id", "realm", "method", "intent", "request")},
            "payload": document["payload"],
        }
        return Case("credential", "credential.format", "valid", document, Verdict(True, "accepted", expected))

    # -- receipts ----------------------------------------------------------

    def receipt_document(self) -> dict[str, Any]:
        return {
            "status": "success",
            "method": self.method(),
            "timestamp": f"2026-{self.rng.randint(1, 12):02d}-{self.rng.randint(1, 28):02d}T12:00:30Z",
            "reference": "0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(64)),
        }

    def receipt_case(self, mutation: str) -> Case:
        document = self.receipt_document()
        if mutation in METHOD_MUTATIONS:
            document["method"] = self.mutate_method(document["method"], mutation)
        token = b64url(json.dumps(document, separators=(",", ":")))
        if mutation in TOKEN_MUTATIONS:
            token = self.mutate_token(token, mutation, ".!\",")
        elif mutation == "inner-space":
            at = self.rng.randint(1, len(token) - 1)
            token = token[:at] + " " + token[at:]
        elif mutation == "not-json":
            token = b64url(self.rng.choice(["hello", "[1,2]", "null", '"receipt"']))
        elif mutation != "valid" and mutation not in METHOD_MUTATIONS:
            raise ValueError(f"unknown receipt mutation {mutation}")
        return Case("receipt", "receipt.parse", mutation, {"header": token}, receipt_verdict(self.grammar, token))

    def receipt_format_case(self) -> Case:
        document = self.receipt_document()
        expected = {key: document[key] for key in ("status", "method", "reference")}
        return Case("receipt", "receipt.format", "valid", document, Verdict(True, "accepted", expected))

    # -- Accept-Payment ----------------------------------------------------

    def accept_payment_case(self, mutation: str) -> Case:
        if mutation == "valid":
            header = self.gen.generate("Accept-Payment", max_length=200)
        else:
            ranges = [f"{self.method()}/{self.intent()}" for _ in range(self.rng.randint(1, 3))]
            if mutation == "method-uppercase":
                ranges[0] = ranges[0].upper()
            elif mutation == "method-digit":
                ranges[0] = ranges[0].replace("/", "2/", 1)
            elif mutation == "method-colon":
                ranges[0] = ranges[0].replace("/", ":x/", 1)
            elif mutation == "missing-slash":
                ranges[0] = ranges[0].replace("/", "", 1)
            elif mutation == "bad-weight":
                ranges[0] += self.rng.choice([";q=2", ";q=0.1234", ";q=", ";q=0.5x"])
            elif mutation == "trailing-comma":
                ranges[-1] += ","
            else:
                raise ValueError(f"unknown Accept-Payment mutation {mutation}")
            header = ", ".join(ranges)
        return Case("accept-payment", None, mutation, {"header": header}, accept_payment_verdict(self.grammar, header))


def _schedule(count: int, mutations: tuple[str, ...]) -> list[str]:
    """Valid cases for VALID_SHARE of the budget, then every mutation in turn."""
    valid = min(count, max(1, round(count * VALID_SHARE)))
    return ["valid"] * valid + [mutations[index % len(mutations)] for index in range(count - valid)]


def generate_cases(grammar: Grammar, *, seed: int, cases: int, kinds: list[str]) -> list[Case]:
    """Generate about `cases` cases per header kind; the same seed gives the same cases."""
    out: list[Case] = []
    for kind in kinds:
        rng = random.Random(int.from_bytes(hashlib.sha256(f"{seed}:{kind}".encode()).digest()[:8], "big"))
        factory = CaseFactory(grammar, rng)
        if kind == "challenge":
            out += [factory.challenge_case(m) for m in _schedule(cases - cases // 5, CHALLENGE_MUTATIONS)]
            out += [factory.challenge_format_case(m) for m in _schedule(cases // 5, METHOD_MUTATIONS)]
        elif kind == "credential":
            out += [factory.credential_case(m) for m in _schedule(cases - cases // 10, CREDENTIAL_MUTATIONS)]
            out += [factory.credential_format_case() for _ in range(cases // 10)]
        elif kind == "receipt":
            out += [factory.receipt_case(m) for m in _schedule(cases - cases // 10, RECEIPT_MUTATIONS)]
            out += [factory.receipt_format_case() for _ in range(cases // 10)]
        elif kind == "accept-payment":
            out += [factory.accept_payment_case(m) for m in _schedule(cases, ACCEPT_PAYMENT_MUTATIONS)]
        else:
            raise ValueError(f"unknown kind {kind}")
    return out


# ---------------------------------------------------------------------------
# Comparing adapters with the grammar and with each other
# ---------------------------------------------------------------------------

FORMAT_OUTPUT_RULES = {
    "challenge.format": "payment-challenge",
    "credential.format": "payment-credentials",
    "receipt.format": "Payment-Receipt",
}
# The full verdict a formatted header must pass, beyond matching its rule.
FORMAT_OUTPUT_VERDICTS: dict[str, Callable[[Grammar, str], Verdict]] = {
    "challenge.format": challenge_verdict,
    "credential.format": lambda grammar, header: credential_verdict(grammar, header, with_request=True),
    "receipt.format": receipt_verdict,
}


def mismatched_fields(expected: dict[str, Any], actual: Any, prefix: str = "") -> list[str]:
    """The expected fields an actual value lacks or holds differently (extra fields are fine)."""
    if not isinstance(actual, dict):
        return [prefix.rstrip(".") or "value"]
    fields: list[str] = []
    for key, value in expected.items():
        if isinstance(value, dict) and key not in ("request", "payload"):
            fields += mismatched_fields(value, actual.get(key), f"{prefix}{key}.")
        elif actual.get(key) != value:
            fields.append(f"{prefix}{key}")
    return fields


def grammar_disagreement(grammar: Grammar, case: Case, response: dict[str, Any]) -> tuple[str, list[str]] | None:
    """How one adapter response disagrees with the grammar's verdict, if it does.

    Kinds: accepts-invalid, rejects-valid, value (accepted, but parsed to other
    values than the grammar gives), invalid-output (formatted a header the
    grammar rejects) and crash (no declared error type, or a timeout).
    """
    assert case.operation is not None
    result = outcome(case.operation, response)
    if result["class"] == "crash":
        return "crash", []
    accepted = result["class"] == "ok"
    if accepted != case.verdict.accept:
        return ("accepts-invalid" if accepted else "rejects-valid"), []
    if not accepted:
        return None
    if case.operation.endswith(".format"):
        header = (response.get("value") or {}).get("header")
        rule = FORMAT_OUTPUT_RULES[case.operation]
        if not isinstance(header, str) or not grammar.accepts(rule, header):
            return "invalid-output", [rule]
        # Matching the rule is not enough: the header must pass the same checks
        # as a parsed one and decode to the values it was formatted from.
        parsed = FORMAT_OUTPUT_VERDICTS[case.operation](grammar, header)
        if parsed.expected is None:
            return "invalid-output", [parsed.reason]
        if case.verdict.expected is None:
            return None
        fields = mismatched_fields(case.verdict.expected, parsed.expected)
        return ("value", fields) if fields else None
    if case.verdict.expected is not None:
        fields = mismatched_fields(case.verdict.expected, result["value"])
        if fields:
            return "value", fields
    return None


@dataclass
class Finding:
    """A group of identical disagreements, with its first example."""

    source: str  # "grammar" or "reference"
    adapter: str
    operation: str
    kind: str
    fields: tuple[str, ...]
    reason: str
    input: Any
    response: dict[str, Any]
    reference: dict[str, Any] | None = None
    allowlisted: str | None = None
    count: int = 0
    mutations: Counter[str] = field(default_factory=Counter)

    def example(self) -> str:
        value = self.input["header"] if isinstance(self.input, dict) and "header" in self.input else self.input
        return _clip(value, 120)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "adapter": self.adapter,
            "operation": self.operation,
            "kind": self.kind,
            "fields": list(self.fields),
            "mutations": dict(self.mutations),
            "count": self.count,
            "grammar": self.reason,
            "input": self.input,
            "response": self.response,
            **({"reference": self.reference} if self.reference is not None else {}),
            **({"allowlisted": self.allowlisted} if self.allowlisted else {}),
        }


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = json.dumps(text, ensure_ascii=False)[1:-1].replace("|", "\\|").replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Comparison:
    """Collects grammar and cross-adapter disagreements over a run."""

    def __init__(self, grammar: Grammar, allowlist: list[AllowlistEntry], reference: str = REFERENCE):
        self.grammar = grammar
        self.allowlist = allowlist
        self.reference = reference
        self.grammar_findings: dict[tuple[Any, ...], Finding] = {}
        self.reference_findings: dict[tuple[Any, ...], Finding] = {}
        self.cases: Counter[str] = Counter()
        self.accepted: Counter[str] = Counter()

    def add(self, case: Case, responses: dict[str, dict[str, Any]]) -> None:
        label = case.operation or case.kind
        self.cases[label] += 1
        self.accepted[label] += case.verdict.accept
        if case.operation is None:
            return
        for adapter, response in sorted(responses.items()):
            disagreement = grammar_disagreement(self.grammar, case, response)
            if disagreement:
                kind, fields = disagreement
                # Verdict disagreements group by mutation, value ones by field.
                key = (adapter, case.operation, kind, tuple(fields), "" if kind == "value" else case.mutation)
                self._record(self.grammar_findings, key, case, Finding(
                    "grammar", adapter, case.operation, kind, tuple(fields), case.verdict.reason, case.input, response,
                ))
        reference = responses.get(self.reference)
        if reference is None:
            return
        expected = self._comparable(case.operation, reference)
        for adapter, response in sorted(responses.items()):
            if adapter == self.reference:
                continue
            classified = classify(expected, self._comparable(case.operation, response))
            if not classified:
                continue
            kind, fields = classified
            divergence = Divergence(case.operation, adapter, kind, fields, case.input, reference, response)
            entry = find_allowlisted(self.allowlist, divergence)
            # Grouped by what diverged, not by error message, which often quotes the input.
            key = (case.operation, adapter, kind, fields, entry.id if entry else "")
            self._record(self.reference_findings, key, case, Finding(
                "reference", adapter, case.operation, kind, tuple(fields), case.verdict.reason, case.input, response,
                reference, entry.id if entry else None,
            ))

    @staticmethod
    def _comparable(operation: str, response: dict[str, Any]) -> dict[str, Any]:
        result = outcome(operation, response)
        if operation.endswith(".format") and result["class"] == "ok":
            # Formatted headers differ harmlessly (quoting, parameter order);
            # the grammar check compares what they parse to.
            return {"class": "ok", "value": None}
        return result

    @staticmethod
    def _record(store: dict[tuple[Any, ...], Finding], key: tuple[Any, ...], case: Case, finding: Finding) -> None:
        existing = store.setdefault(key, finding)
        existing.count += 1
        existing.mutations[case.mutation] += 1

    @property
    def new_reference_findings(self) -> list[Finding]:
        return [finding for finding in self.reference_findings.values() if not finding.allowlisted]

    @property
    def disagreements(self) -> int:
        return len(self.grammar_findings) + len(self.new_reference_findings)

    def summary(self, *, seed: int, cases: int, adapters: list[str], specs: str, build_errors: dict[str, str]) -> str:
        lines = [
            "## ABNF differential fuzzing",
            "",
            f"Seed {seed}, {cases} cases per header kind, adapters {', '.join(adapters)}, grammar from {specs}.",
            "",
            "Cases (accepted by the grammar): "
            + ", ".join(f"{label} {count} ({self.accepted[label]})" for label, count in sorted(self.cases.items()))
            + ". Accept-Payment has no adapter operation, so its cases only exercise the grammar.",
        ]
        for name, error in sorted(build_errors.items()):
            lines += ["", f"Adapter {name} did not start: {_clip(error, 300)}"]
        lines += ["", "### Adapters disagreeing with the grammar", ""]
        if not self.grammar_findings:
            lines.append("None.")
        else:
            lines += [
                "| Adapter | Operation | Disagreement | Mutations | Cases | Example | Grammar verdict |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            for finding in sorted(self.grammar_findings.values(), key=lambda f: (f.adapter, f.operation, f.kind, sorted(f.mutations))):
                kind = finding.kind + (f" ({', '.join(finding.fields)})" if finding.fields else "")
                lines.append(
                    f"| {finding.adapter} | {finding.operation} | {kind} | {', '.join(sorted(finding.mutations))} "
                    f"| {finding.count} | `{finding.example()}` | {finding.reason} |"
                )
        lines += ["", f"### Adapters disagreeing with {self.reference} (not in fuzz/allowlist.json)", ""]
        new = self.new_reference_findings
        if not new:
            lines.append("None.")
        else:
            lines += ["| Adapter | Operation | Divergence | Mutations | Cases | Example |", "| --- | --- | --- | --- | --- | --- |"]
            for finding in sorted(new, key=lambda f: (f.adapter, f.operation, f.kind, sorted(f.mutations))):
                kind = finding.kind + (f" ({', '.join(finding.fields)})" if finding.fields else "")
                lines.append(
                    f"| {finding.adapter} | {finding.operation} | {kind} | {', '.join(sorted(finding.mutations))} "
                    f"| {finding.count} | `{finding.example()}` |"
                )
        allowlisted: Counter[str] = Counter()
        for finding in self.reference_findings.values():
            if finding.allowlisted:
                allowlisted[finding.allowlisted] += finding.count
        if allowlisted:
            lines += ["", "Allowlisted: " + ", ".join(f"{key} {count}" for key, count in sorted(allowlisted.items())) + "."]
        lines += ["", f"Reproduce: `cd conformance && make abnf-fuzz ABNF_FUZZ_ARGS='--seed {seed} --cases {cases}'`"]
        return "\n".join(lines) + "\n"

    def report(self, *, seed: int, cases: int, adapters: list[str], specs: str, build_errors: dict[str, str]) -> dict[str, Any]:
        return {
            "seed": seed,
            "casesPerKind": cases,
            "adapters": adapters,
            "specs": specs,
            "buildErrors": build_errors,
            "cases": dict(self.cases),
            "acceptedByGrammar": dict(self.accepted),
            "grammarDisagreements": [finding.to_json() for finding in self.grammar_findings.values()],
            "referenceDivergences": [finding.to_json() for finding in self.reference_findings.values()],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class Runner:
    """Send each case to every adapter that supports its operation, in parallel."""

    def __init__(self, adapters: dict[str, AdapterConfig]):
        self.sessions = {name: AdapterSession(config) for name, config in adapters.items()}
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.sessions)))

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        for session in self.sessions.values():
            session.close()

    def _call(self, name: str, operation: str, input_value: Any) -> dict[str, Any]:
        response: dict[str, Any] = {}
        for attempt in range(2):
            try:
                response = self.sessions[name].call(operation, input_value, timeout=CALL_TIMEOUT, validate_input=False)
            except Exception as exc:  # noqa: BLE001 - a response that fails the operation schema.
                return {"ok": False, "error": {"type": "unknown_error", "message": str(exc)}}
            message = error_message(response)
            # One retry on a fresh process, so a loaded machine does not read as a crash.
            if attempt == 0 and (message == "Timeout" or message.startswith("Adapter process")):
                continue
            return response
        return response

    def run(self, case: Case) -> dict[str, dict[str, Any]]:
        if case.operation is None:
            return {}
        names = [name for name, session in self.sessions.items() if case.operation in session.adapter.capabilities]
        futures = {name: self.pool.submit(self._call, name, case.operation, case.input) for name in names}
        return {name: future.result() for name, future in futures.items()}


def clone_specs(destination: Path, ref: str) -> Path:
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--branch", ref, SPECS_REPOSITORY, str(destination)],
        check=True,
    )
    return destination


def load_grammar(specs: Path | None, ref: str) -> tuple[Grammar, str]:
    """The grammar from a specs checkout (cloned when None), and a label naming its revision."""
    with tempfile.TemporaryDirectory(prefix="mpp-specs-") as directory:
        specs_dir = specs or clone_specs(Path(directory) / "mpp-specs", ref)
        grammar = Grammar.from_specs(specs_dir)
        revision = subprocess.run(
            ["git", "-C", str(specs_dir), "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, check=False,
        ).stdout.strip()
    return grammar, f"tempoxyz/mpp-specs@{revision}" if revision else str(specs_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Grammar-based differential fuzzing of conformance adapters.")
    parser.add_argument("--specs", type=Path, help="mpp-specs checkout; clones tempoxyz/mpp-specs when omitted")
    parser.add_argument("--specs-ref", default="main", help="Branch or tag to clone without --specs (default: main)")
    parser.add_argument("--adapters", help="Comma-separated adapters (default: all; the reference always runs)")
    parser.add_argument("--kinds", default=",".join(KINDS), help=f"Comma-separated header kinds (default: {','.join(KINDS)})")
    parser.add_argument("--seed", type=int, default=0, help="Seed for case generation (default: 0)")
    parser.add_argument("--cases", type=int, default=200, help="Cases per header kind (default: 200)")
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST_PATH, help="Known differences from the reference")
    parser.add_argument("--report", type=Path, help="Write a JSON report to this path")
    parser.add_argument("--summary", type=Path, help="Write a Markdown summary to this path")
    parser.add_argument("--report-only", action="store_true", help="Exit 0 even when adapters disagree")
    parser.add_argument("--build", action="store_true", help="Run adapter build commands first")
    args = parser.parse_args(argv)
    if args.cases < 1:
        parser.error("--cases must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    from harness import build_adapter

    args = parse_args(argv)
    kinds = args.kinds.split(",")
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        print(f"Unknown kinds: {', '.join(unknown)}", file=sys.stderr)
        return 2
    discovered = discover_adapters()
    names = args.adapters.split(",") if args.adapters else sorted(discovered)
    unknown = sorted(set(names) - set(discovered))
    if unknown:
        print(f"Unknown adapters: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if REFERENCE not in names:
        names.append(REFERENCE)

    grammar, specs_label = load_grammar(args.specs, args.specs_ref)
    cases = generate_cases(grammar, seed=args.seed, cases=args.cases, kinds=kinds)

    adapters: dict[str, AdapterConfig] = {}
    build_errors: dict[str, str] = {}
    for name in names:
        error = build_adapter(discovered[name]) if args.build else None
        if not error:
            probe = AdapterClient(discovered[name]).call("base64url.encode", {"text": "a"})
            if probe.get("ok") is not True:
                error = f"adapter did not start: {error_message(probe)[:300]}"
        if error:
            build_errors[name] = error
            print(f"✗ {name}: {error}", file=sys.stderr)
        else:
            adapters[name] = discovered[name]
    if REFERENCE not in adapters:
        return 1

    print(f"abnf-fuzz seed={args.seed} cases={len(cases)} adapters={','.join(sorted(adapters))} grammar={specs_label}", flush=True)
    comparison = Comparison(grammar, load_allowlist(args.allowlist))
    runner = Runner(adapters)
    try:
        for case in cases:
            comparison.add(case, runner.run(case))
    finally:
        runner.close()

    meta = {"seed": args.seed, "cases": args.cases, "adapters": sorted(adapters), "specs": specs_label, "build_errors": build_errors}
    summary = comparison.summary(**meta)
    print("\n" + summary)
    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(comparison.report(**meta), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failed = comparison.disagreements > 0 or bool(build_errors)
    return 0 if args.report_only or not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
