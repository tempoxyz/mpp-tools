#!/usr/bin/env python3
"""
Differential fuzzer for MPP conformance adapters.

Generates protocol inputs with Hypothesis, sends each one to every selected
adapter and to the TypeScript reference adapter (mppx), and compares the
normalized results: the same success or failure class, and the same parsed
value. Each new divergence is shrunk to a minimal input and printed as a
vector scenario ready to paste into vectors/. Known, accepted differences live
in fuzz/allowlist.json.

Usage:
    uv run --locked python scripts/diff_fuzz.py [--adapters python,go]
        [--budget SECONDS] [--max-examples N] [--seed N] [--operations OPS] [--report PATH]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import string
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, Phase, Verbosity, given, seed, settings
from hypothesis import strategies as st

from harness import CONFORMANCE_DIR, AdapterClient, AdapterConfig, AdapterSession, discover_adapters


REFERENCE = "typescript"
ALLOWLIST_PATH = CONFORMANCE_DIR / "fuzz" / "allowlist.json"
OPERATIONS = (
    "challenge.parse",
    "challenge.format",
    "credential.parse",
    "credential.format",
    "receipt.parse",
    "receipt.format",
    "base64url.encode",
    "base64url.decode",
    "challenge.id",
)
PARSE_FOR_FORMAT = {
    "challenge.format": "challenge.parse",
    "credential.format": "credential.parse",
    "receipt.format": "receipt.parse",
}
KINDS = ("accepts", "rejects", "value", "crash", "reference_crash")
CATEGORIES = ("harmless", "sdk-bug", "adapter-bug")
# Values compared whole: a difference inside them is one field, not one per key.
OPAQUE_FIELDS = {"request", "payload", "extra", "source"}
VECTOR_FILES = {
    "challenge.parse": "www-authenticate.json",
    "challenge.format": "www-authenticate.json",
    "credential.parse": "authorization.json",
    "credential.format": "authorization.json",
    "receipt.parse": "receipt.json",
    "receipt.format": "receipt.json",
    "base64url.encode": "base64url.json",
    "base64url.decode": "base64url.json",
    "challenge.id": "challenge-id.json",
}
ERROR_TYPES = {
    "parse": "parse_error",
    "format": "format_error",
    "base64url": "encoding_error",
    "challenge.id": "generation_error",
}
CALL_TIMEOUT = 20.0
CHUNK_EXAMPLES = 25
# Shrinking stops this long after a batch first diverges, keeping the best so far.
SHRINK_SECONDS = 20.0


# ---------------------------------------------------------------------------
# Normalization and comparison
# ---------------------------------------------------------------------------


def b64url(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


REQUIRED_FIELDS = {
    "challenge": {"id", "realm", "method", "intent", "request"},
    "credential": {"challenge", "payload"},
    "receipt": {"status", "timestamp", "reference"},
}


def _drop_empty(value: Any, required: set[str]) -> Any:
    """Drop optional fields that are null, "" or {}: SDKs differ in omitting them."""
    if not isinstance(value, dict):
        return value
    return {
        key: item
        for key, item in value.items()
        if key in required or item not in (None, "", {})
    }


def _numbers(value: Any) -> Any:
    """Compare integers beyond 2**53 as the doubles JavaScript reads them as."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > 2**53:
        return float(value)
    if isinstance(value, dict):
        return {key: _numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numbers(item) for item in value]
    return value


def _decode_request(challenge: Any) -> Any:
    if not isinstance(challenge, dict) or not isinstance(challenge.get("request"), str):
        return challenge
    try:
        encoded = challenge["request"]
        decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except Exception:
        return challenge
    return {**challenge, "request": decoded}


def normalize_value(operation: str, value: Any) -> Any:
    """Normalize one successful adapter value for comparison.

    Optional fields that are null, "" or {} count as absent, a credential's
    challenge request compares decoded, as the vector runner does, and
    integers beyond 2**53 compare as doubles.
    """
    value = _numbers(value)
    if operation == "challenge.parse":
        return _numbers(_decode_request(_drop_empty(value, REQUIRED_FIELDS["challenge"])))
    if operation == "credential.parse":
        credential = _drop_empty(value, REQUIRED_FIELDS["credential"])
        if isinstance(credential, dict) and isinstance(credential.get("challenge"), dict):
            challenge = _drop_empty(credential["challenge"], REQUIRED_FIELDS["challenge"])
            credential = {**credential, "challenge": _numbers(_decode_request(challenge))}
        return credential
    if operation == "receipt.parse":
        return _drop_empty(value, REQUIRED_FIELDS["receipt"])
    return value


def outcome(operation: str, response: dict[str, Any]) -> dict[str, Any]:
    """Reduce an adapter response to its class and normalized value.

    Declared errors compare by class only; messages differ between SDKs.
    Timeouts, crashes, undeclared error types and invalid responses are crashes.
    """
    if response.get("ok") is True:
        return {"class": "ok", "value": normalize_value(operation, response.get("value"))}
    error_type = (response.get("error") or {}).get("type")
    if error_type in (None, "unknown_error", "unsupported_operation"):
        return {"class": "crash", "message": (response.get("error") or {}).get("message", "")}
    return {"class": "error", "type": error_type}


_MISSING = object()


def diff_fields(expected: Any, actual: Any, prefix: str = "") -> tuple[str, ...]:
    """Name the fields where two normalized values differ."""
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return () if expected == actual else (prefix.rstrip(".") or "value",)
    fields: list[str] = []
    for key in sorted(set(expected) | set(actual), key=str):
        left, right = expected.get(key, _MISSING), actual.get(key, _MISSING)
        if left == right:
            continue
        if left is _MISSING or right is _MISSING or key in OPAQUE_FIELDS:
            fields.append(f"{prefix}{key}")
        else:
            fields.extend(diff_fields(left, right, f"{prefix}{key}."))
    return tuple(fields)


def classify(reference: dict[str, Any], actual: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    """Classify how an adapter outcome diverges from the reference, if it does."""
    ref_class, act_class = reference["class"], actual["class"]
    if ref_class == "crash" and act_class == "crash":
        return None
    if act_class == "crash":
        return "crash", ()
    if ref_class == "crash":
        return "reference_crash", ()
    if ref_class == "error" and act_class == "ok":
        return "accepts", ()
    if ref_class == "ok" and act_class == "error":
        return "rejects", ()
    if ref_class == "ok" and reference["value"] != actual["value"]:
        return "value", diff_fields(reference["value"], actual["value"])
    return None


def error_message(response: dict[str, Any]) -> str:
    return str((response.get("error") or {}).get("message", ""))


def error_cause(message: str) -> str:
    """Reduce an error message to the cause it names, without input values.

    Divergences with different causes are tracked separately, so one known
    rejection does not hide another. mppx validation errors are JSON issue
    lists; they reduce to their paths and codes.
    """
    try:
        issues = json.loads(message)
    except (ValueError, TypeError):
        issues = None
    if isinstance(issues, list) and issues and all(isinstance(issue, dict) for issue in issues):
        return ",".join(
            f"{'.'.join(str(part) for part in issue.get('path', []))}:{issue.get('code', '')}" for issue in issues
        )
    message = " ".join(message.split())
    return re.sub(r'"[^"]{0,40}"|\'[^\']{0,40}\'|\b\d+\b', "_", message)[:100]


def input_text(operation: str, input_value: Any) -> str:
    """The text an allowlist inputPattern is matched against."""
    if isinstance(input_value, dict):
        if operation.endswith(".parse") and isinstance(input_value.get("header"), str):
            return input_value["header"]
        if operation.startswith("base64url.") and isinstance(input_value.get("text"), str):
            return input_value["text"]
    return json.dumps(input_value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Divergence:
    operation: str
    adapter: str
    kind: str
    fields: tuple[str, ...]
    input: Any = field(compare=False)
    reference: dict[str, Any] = field(compare=False)
    actual: dict[str, Any] = field(compare=False)

    @property
    def message(self) -> str:
        """The error message of the side that failed, if one did."""
        failed = self.reference if self.kind in ("accepts", "reference_crash") else self.actual
        return error_message(failed) if failed.get("ok") is not True else ""

    @property
    def signature(self) -> tuple[str, ...]:
        return (self.operation, self.adapter, self.kind, *self.fields, error_cause(self.message))

    def describe(self) -> str:
        fields = f" ({', '.join(self.fields)})" if self.fields else ""
        return f"{self.adapter} {self.operation} {self.kind}{fields}"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowlistEntry:
    id: str
    adapters: tuple[str, ...]
    operations: tuple[str, ...]
    kinds: tuple[str, ...]
    category: str
    reason: str
    input_pattern: re.Pattern[str] | None = None
    message_pattern: re.Pattern[str] | None = None
    fields: tuple[str, ...] | None = None

    def matches(self, divergence: Divergence) -> bool:
        if "*" not in self.adapters and divergence.adapter not in self.adapters:
            return False
        if divergence.operation not in self.operations or divergence.kind not in self.kinds:
            return False
        if self.fields is not None and not set(divergence.fields) <= set(self.fields):
            return False
        if self.message_pattern is not None and not self.message_pattern.search(divergence.message):
            return False
        if self.input_pattern is not None:
            return bool(self.input_pattern.search(input_text(divergence.operation, divergence.input)))
        return True


def parse_allowlist(document: Any, label: str = "allowlist") -> list[AllowlistEntry]:
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        raise ValueError(f"{label} must be an object with an entries list")
    entries: list[AllowlistEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(document["entries"]):
        where = f"{label} entry {index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object")
        allowed = {
            "id", "adapters", "operations", "kinds", "category", "reason", "inputPattern", "messagePattern", "fields",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
        for key in ("id", "category", "reason"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"{where} requires a non-empty {key}")
        if raw["id"] in seen:
            raise ValueError(f"{where} duplicates id {raw['id']}")
        seen.add(raw["id"])
        if raw["category"] not in CATEGORIES:
            raise ValueError(f"{where} category must be one of {', '.join(CATEGORIES)}")
        lists: dict[str, tuple[str, ...]] = {}
        for key, choices in (("adapters", None), ("operations", OPERATIONS), ("kinds", KINDS)):
            values = raw.get(key)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{where} requires a non-empty {key} list")
            if choices is not None and set(values) - set(choices):
                bad = ", ".join(sorted(set(values) - set(choices)))
                raise ValueError(f"{where} has unknown {key}: {bad}")
            lists[key] = tuple(values)
        patterns: dict[str, re.Pattern[str] | None] = {}
        for key in ("inputPattern", "messagePattern"):
            patterns[key] = None
            if key in raw:
                try:
                    patterns[key] = re.compile(raw[key])
                except (re.error, TypeError) as exc:
                    raise ValueError(f"{where} {key} is invalid: {exc}") from exc
        fields = raw.get("fields")
        if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)):
            raise ValueError(f"{where} fields must be a list of strings")
        entries.append(AllowlistEntry(
            id=raw["id"],
            adapters=lists["adapters"],
            operations=lists["operations"],
            kinds=lists["kinds"],
            category=raw["category"],
            reason=raw["reason"],
            input_pattern=patterns["inputPattern"],
            message_pattern=patterns["messagePattern"],
            fields=tuple(fields) if fields is not None else None,
        ))
    return entries


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[AllowlistEntry]:
    return parse_allowlist(json.loads(path.read_text(encoding="utf-8")), str(path))


def find_allowlisted(entries: list[AllowlistEntry], divergence: Divergence) -> AllowlistEntry | None:
    """The entry that accepts a divergence, if any.

    A value divergence in several fields is accepted when each field on its
    own is, so independent known differences compose.
    """
    entry = next((entry for entry in entries if entry.matches(divergence)), None)
    if entry or divergence.kind != "value" or len(divergence.fields) < 2:
        return entry
    covering = [find_allowlisted(entries, replace(divergence, fields=(name,))) for name in divergence.fields]
    return covering[0] if all(covering) else None


# ---------------------------------------------------------------------------
# Reproducers
# ---------------------------------------------------------------------------


def _error_expectation(operation: str) -> dict[str, Any]:
    if operation == "challenge.id":
        error_type = ERROR_TYPES["challenge.id"]
    elif operation.startswith("base64url."):
        error_type = ERROR_TYPES["base64url"]
    else:
        error_type = ERROR_TYPES[operation.rsplit(".", 1)[1]]
    return {"success": False, "error_type": error_type}


def vector_scenario(divergence: Divergence) -> dict[str, Any] | None:
    """Render a divergence as a vector scenario expecting the reference result.

    None when the reference crashed: there is no reference result to expect.
    """
    if divergence.kind == "reference_crash":
        return None
    op = divergence.operation
    digest = hashlib.sha256(json.dumps(divergence.input, sort_keys=True).encode()).hexdigest()[:8]
    reference = divergence.reference
    ok = reference.get("ok") is True
    value = reference.get("value")
    scenario: dict[str, Any] = {
        "name": f"fuzz_{divergence.adapter}_{divergence.kind}_{digest}",
        "description": f"diff-fuzz: {divergence.describe()} against {REFERENCE}",
        "tags": ["fuzz", "edge-case"],
    }
    expectation: Any = True if ok else _error_expectation(op)
    if op == "challenge.id":
        scenario["input"] = divergence.input
        if ok:
            scenario["expected"] = value["id"]
        else:
            scenario["tests"] = {"generate": expectation}
    elif op == "base64url.decode":
        scenario["encoded"] = divergence.input["text"]
        if ok:
            scenario["decoded"] = value["text"]
        scenario["tests"] = {"parse": expectation}
    elif op == "base64url.encode":
        scenario["decoded"] = divergence.input["text"]
        if ok:
            scenario["encoded"] = value["text"]
        scenario["tests"] = {"format": expectation}
    elif op.endswith(".parse"):
        scenario["wire"] = divergence.input["header"]
        if ok:
            scenario["object"] = value
        scenario["tests"] = {"parse": expectation}
    else:
        scenario["object"] = divergence.input
        if ok:
            scenario["wire"] = value["header"]
        if divergence.kind == "value":
            # The fuzzer compares headers by what the reference parses them
            # to, while a semantic format test parses with each adapter's own
            # parser, which may hide the difference. Compare the wire exactly,
            # for the two adapters that diverged.
            expectation = "exact"
            scenario["adapters"] = [REFERENCE, divergence.adapter]
        scenario["tests"] = {"format": expectation}
    return scenario


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

TOKEN_CHARS = string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~"
B64URL_CHARS = string.ascii_letters + string.digits + "-_"
INTERESTING_CHARS = ['"', "\\", ",", "=", " ", "\t", "\r\n", "\n", ";", "\x00", "\x7f", "\u00e9", "\u2028", "\U0001f600", "\ufeff"]
DECOYS = ['Payment ', 'payment=', ' Payment id="decoy"', 'Basic realm="Payment id=\\"x\\""', "Bearer, "]

identifier = st.from_regex(r"\A[a-z][a-z0-9_-]{0,9}\Z")
token = st.text(alphabet=TOKEN_CHARS, min_size=1, max_size=12)
any_text = st.text(max_size=24)
header_text = st.one_of(
    st.text(alphabet=string.ascii_letters + string.digits + " .:/-_", max_size=24),
    st.text(st.characters(codec="utf-8"), max_size=16),
    st.lists(st.sampled_from(INTERESTING_CHARS + ["a", "Payment"]), max_size=8).map("".join),
)
json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    any_text,
)
json_value = st.recursive(
    json_scalar,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(any_text, children, max_size=4),
    max_leaves=12,
)
json_object = st.dictionaries(
    st.one_of(st.sampled_from(["amount", "currency", "recipient", "chainId", "methodDetails", "externalId"]), any_text),
    json_value,
    max_size=5,
)
timestamp = st.datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2999, 12, 31),
).flatmap(
    lambda moment: st.sampled_from([
        moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z",
    ])
)


@st.composite
def challenges(draw: st.DrawFn) -> dict[str, Any]:
    challenge: dict[str, Any] = {
        "id": draw(st.one_of(token, header_text.filter(bool))),
        "realm": draw(st.one_of(st.sampled_from(["api.example.com", ""]), header_text)),
        "method": draw(identifier),
        "intent": draw(identifier),
        "request": draw(json_object),
    }
    optional = {
        "description": header_text,
        "digest": st.binary(min_size=32, max_size=32).map(lambda raw: "sha-256=" + base64.b64encode(raw).decode()),
        "expires": timestamp,
        # Servers emit opaque as JCS of a string map; keep it canonical.
        "opaque": st.dictionaries(any_text, any_text, max_size=3).map(
            lambda value: b64url(json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True))
        ),
    }
    for key, strategy in optional.items():
        if draw(st.booleans()):
            challenge[key] = draw(strategy)
    return challenge


@st.composite
def credentials(draw: st.DrawFn) -> dict[str, Any]:
    credential: dict[str, Any] = {"challenge": draw(challenges()), "payload": draw(json_object)}
    source = draw(st.one_of(st.nothing(), st.none(), header_text, json_object) | st.just(_MISSING))
    if source is not _MISSING:
        credential["source"] = source
    return credential


@st.composite
def receipts(draw: st.DrawFn) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "status": draw(st.one_of(st.just("success"), token)),
        "timestamp": draw(timestamp),
        "reference": draw(st.one_of(token, header_text.filter(bool))),
    }
    receipt["method"] = draw(identifier)
    optional = {
        "externalId": header_text,
        "subscriptionId": token,
        "extra": json_object,
        "originTxHash": header_text,
    }
    for key, strategy in optional.items():
        if draw(st.booleans()):
            receipt[key] = draw(strategy)
    return receipt


def quote(value: str, escape_all: bool = False) -> str:
    if escape_all:
        return '"' + "".join("\\" + char for char in value) + '"'
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


huge_json = st.sampled_from([
    "[" * 5000 + "]" * 5000,
    '{"a":' * 2000 + "1" + "}" * 2000,
    json.dumps({"amount": "1" * 70000}),
    json.dumps({f"k{i}": i for i in range(5000)}),
])
request_param = st.one_of(
    json_object.map(lambda value: b64url(json.dumps(value, separators=(",", ":")))),
    json_value.map(lambda value: b64url(json.dumps(value))),
    json_object.map(lambda value: base64.b64encode(json.dumps(value).encode()).decode()),
    huge_json.map(b64url),
    st.binary(max_size=24).map(b64url),
    st.text(alphabet=B64URL_CHARS + "=+/. ", max_size=24),
    st.sampled_from(['e30', 'e30=', 'e30==', 'eyJhIjoxLCJhIjoyfQ', '', 'bnVsbA', 'W10']),
)


@st.composite
def challenge_headers(draw: st.DrawFn) -> str:
    """Build a WWW-Authenticate value from a challenge with adversarial framing."""
    params = {key: value for key, value in draw(challenges()).items() if key != "request"}
    params["request"] = draw(request_param)
    items = [(key, value) for key, value in params.items() if draw(st.integers(0, 9)) > 0]
    items += draw(st.lists(st.tuples(st.sampled_from(["id", "realm", "x-ext", "Request", "ID"]), header_text), max_size=2))
    items = draw(st.permutations(items))
    parts = []
    for key, value in items:
        if all(char in TOKEN_CHARS for char in value) and value and draw(st.booleans()):
            rendered = value
        else:
            rendered = quote(value, escape_all=draw(st.integers(0, 9)) == 0)
        eq = draw(st.sampled_from(["=", "=", " = ", "= "]))
        parts.append(f"{key}{eq}{rendered}")
    sep = draw(st.sampled_from([", ", ",", " , ", ",, ", " "]))
    scheme = draw(st.sampled_from(["Payment", "Payment", "payment", "PAYMENT", "PayMent", "Payments"]))
    space = draw(st.sampled_from([" ", " ", "  ", "\t", ""]))
    header = f"{scheme}{space}{sep.join(parts)}"
    prefix = draw(st.sampled_from(["", "", "", "Basic realm=\"x\", ", "Bearer, ", 'Basic realm="Payment id=\\"x\\"", ']))
    suffix = draw(st.sampled_from(["", "", "", ", Basic realm=\"y\"", ",", ", Payment id=\"second\""]))
    return mutate(draw, prefix + header + suffix)


def mutate(draw: st.DrawFn, text: str) -> str:
    """Apply up to three small edits that tend to expose tokenizer differences."""
    for _ in range(draw(st.integers(0, 3))):
        position = draw(st.integers(0, len(text)))
        edit = draw(st.sampled_from(["insert", "delete", "decoy"]))
        if edit == "insert":
            text = text[:position] + draw(st.sampled_from(INTERESTING_CHARS)) + text[position:]
        elif edit == "delete":
            text = text[:position] + text[position + 1:]
        else:
            text = text[:position] + draw(st.sampled_from(DECOYS)) + text[position:]
    return text


def credential_wire(credential: dict[str, Any]) -> dict[str, Any]:
    """The JSON a credential header carries: the challenge request base64url-encoded."""
    challenge = dict(credential["challenge"])
    challenge["request"] = b64url(json.dumps(challenge["request"], separators=(",", ":")))
    return {**credential, "challenge": challenge}


def _tweak_json(draw: st.DrawFn, value: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(value))
    target = value["challenge"] if "challenge" in value and draw(st.booleans()) else value
    if not isinstance(target, dict) or not target:
        return value
    key = draw(st.sampled_from(sorted(target)))
    action = draw(st.sampled_from(["drop", "replace", "add"]))
    if action == "drop":
        del target[key]
    elif action == "replace":
        target[key] = draw(json_value)
    else:
        target[draw(any_text)] = draw(json_value)
    return value


@st.composite
def base64_json_headers(draw: st.DrawFn, documents: st.SearchStrategy[dict[str, Any]], scheme: bool) -> str:
    """Build an Authorization or Payment-Receipt value around base64url JSON."""
    document = draw(documents)
    if draw(st.booleans()):
        document = _tweak_json(draw, document)
    raw = json.dumps(document, separators=(",", ":"), ensure_ascii=draw(st.booleans()))
    encoded = draw(st.one_of(
        st.just(b64url(raw)),
        st.just(b64url(raw)),
        st.just(base64.b64encode(raw.encode()).decode()),
        st.just(b64url(raw) + "=="),
        huge_json.map(b64url),
        st.binary(max_size=24).map(b64url),
        st.text(alphabet=B64URL_CHARS + "=+/. ", max_size=24),
    ))
    if scheme:
        prefix = draw(st.sampled_from(["Payment ", "Payment ", "payment ", "PAYMENT ", "Payment  ", "Payment\t", "Payment", "Bearer "]))
        encoded = prefix + encoded + draw(st.sampled_from(["", "", ", Basic realm=\"x\"", ","]))
    return mutate(draw, encoded)


def header_input(strategy: st.SearchStrategy[str]) -> st.SearchStrategy[dict[str, str]]:
    return strategy.map(lambda header: {"header": header})


def text_input(strategy: st.SearchStrategy[str]) -> st.SearchStrategy[dict[str, str]]:
    return strategy.map(lambda text: {"text": text})


# The credential header a challenge names. Authorization is the default and
# binds like no header; any other value is inserted before opaque.
credential_header = st.one_of(
    st.sampled_from(["Authorization", "Payment-Authorization", "authorization", "PAYMENT-AUTHORIZATION"]),
    token,
    header_text.filter(bool),
)


@st.composite
def challenge_id_inputs(draw: st.DrawFn) -> dict[str, Any]:
    challenge = draw(challenges())
    params: dict[str, Any] = {
        "secretKey": draw(st.one_of(st.just("test-vector-secret-minimum-32-byte-secret"), st.text(min_size=1, max_size=48))),
        "realm": challenge["realm"],
        "method": challenge["method"],
        "intent": challenge["intent"],
        "request": challenge["request"],
    }
    for key in ("expires", "digest", "opaque"):
        if key in challenge:
            params[key] = challenge[key]
    if draw(st.booleans()):
        params["header"] = draw(credential_header)
    return params


STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    "challenge.parse": header_input(st.one_of(
        challenge_headers(),
        st.text(max_size=48),
    )),
    "challenge.format": challenges(),
    "credential.parse": header_input(st.one_of(
        base64_json_headers(credentials().map(credential_wire), scheme=True),
        st.text(max_size=48),
    )),
    "credential.format": credentials(),
    "receipt.parse": header_input(st.one_of(
        base64_json_headers(receipts(), scheme=False),
        st.text(max_size=48),
    )),
    "receipt.format": receipts(),
    "base64url.encode": text_input(st.text(max_size=64)),
    "base64url.decode": text_input(st.one_of(
        st.text(max_size=64).map(b64url),
        st.binary(max_size=48).map(b64url),
        st.text(alphabet=B64URL_CHARS + "=+/ \n", max_size=48),
        st.text(max_size=24),
    )),
    "challenge.id": challenge_id_inputs(),
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class DivergenceFound(Exception):
    pass


class Fuzzer:
    def __init__(
        self,
        adapters: dict[str, AdapterConfig],
        allowlist: list[AllowlistEntry],
        *,
        reference: str = REFERENCE,
        verbose: bool = False,
    ):
        self.reference = reference
        self.verbose = verbose
        self.sessions = {name: AdapterSession(config) for name, config in adapters.items()}
        self.allowlist = allowlist
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.sessions)))
        self.findings: list[Divergence] = []
        self.known: set[tuple[str, ...]] = set()
        self.allowlisted: Counter[str] = Counter()
        self.cases: Counter[str] = Counter()

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        for session in self.sessions.values():
            session.close()

    def _call(self, name: str, operation: str, input_value: Any) -> dict[str, Any]:
        for attempt in range(2):
            try:
                response = self.sessions[name].call(
                    operation, input_value, timeout=CALL_TIMEOUT, validate_input=False
                )
            except Exception as exc:
                # Responses that fail the operation's schema land here.
                return {"ok": False, "error": {"type": "unknown_error", "message": str(exc)}}
            message = error_message(response)
            # A timeout or dead process gets one retry on a fresh process, so a
            # loaded machine does not read as a crash.
            if attempt == 0 and (message == "Timeout" or message.startswith("Adapter process")):
                continue
            return response
        return response

    def check(self, operation: str, input_value: Any) -> list[Divergence]:
        """Run one input on every adapter and return its divergences from the reference."""
        self.cases[operation] += 1
        names = [name for name, s in self.sessions.items() if operation in s.adapter.capabilities]
        futures = {name: self.pool.submit(self._call, name, operation, input_value) for name in names}
        responses = {name: future.result() for name, future in futures.items()}
        outcomes = {name: self._outcome(operation, response) for name, response in responses.items()}
        reference = outcomes[self.reference]
        divergences = []
        for name in names:
            if name == self.reference:
                continue
            classified = classify(reference, outcomes[name])
            if classified:
                kind, fields = classified
                divergences.append(Divergence(
                    operation, name, kind, fields, input_value, responses[self.reference], responses[name],
                ))
        parse_op = PARSE_FOR_FORMAT.get(operation)
        if parse_op and responses[self.reference].get("ok"):
            divergences += self.check(parse_op, responses[self.reference]["value"])
        return divergences

    def _outcome(self, operation: str, response: dict[str, Any]) -> dict[str, Any]:
        result = outcome(operation, response)
        parse_op = PARSE_FOR_FORMAT.get(operation)
        if parse_op and result["class"] == "ok":
            # Formatted headers compare by what the reference parses them to.
            parsed = outcome(parse_op, self._call(self.reference, parse_op, response["value"]))
            result["value"] = parsed.get("value", {"unparseable": response["value"]["header"]})
        return result

    def run_chunk(self, operation: str, examples: int, chunk_seed: int, deadline: float) -> None:
        """Run one Hypothesis batch, shrinking and recording the first new divergence."""
        state: dict[str, Any] = {}

        def body(input_value: Any) -> None:
            pinned = state.get("signature")
            if pinned is None and time.monotonic() > deadline:
                return
            if pinned is not None and time.monotonic() > state["shrink_deadline"]:
                return
            fresh = []
            for divergence in self.check(operation, input_value):
                entry = find_allowlisted(self.allowlist, divergence)
                if entry:
                    if pinned is None:
                        self.allowlisted[entry.id] += 1
                elif divergence.signature not in self.known:
                    fresh.append(divergence)
            if pinned is not None:
                fresh = [d for d in fresh if d.signature == pinned]
            if fresh:
                now = time.monotonic()
                state.setdefault("shrink_deadline", min(now + SHRINK_SECONDS, max(deadline, now + 5)))
                state["signature"] = fresh[0].signature
                state["last"] = fresh[0]
                raise DivergenceFound(fresh[0].describe())

        test = settings(
            max_examples=examples,
            deadline=None,
            database=None,
            derandomize=False,
            phases=[Phase.generate, Phase.shrink],
            suppress_health_check=list(HealthCheck),
            verbosity=Verbosity.quiet,
            report_multiple_bugs=False,
        )(given(STRATEGIES[operation])(body))
        test = seed(chunk_seed)(test)
        try:
            test()
        except Exception:
            last = state.get("last")
            if last is None:
                raise
            self.known.add(last.signature)
            self.findings.append(last)

    def run(self, operations: list[str], *, budget: float | None, max_examples: int | None, base_seed: int) -> None:
        deadline = time.monotonic() + budget if budget is not None else float("inf")
        done: Counter[str] = Counter()
        round_index = 0
        while time.monotonic() < deadline:
            pending = [op for op in operations if max_examples is None or done[op] < max_examples]
            if not pending:
                break
            for operation in pending:
                if time.monotonic() >= deadline:
                    break
                examples = CHUNK_EXAMPLES if max_examples is None else min(CHUNK_EXAMPLES, max_examples - done[operation])
                chunk_seed = int.from_bytes(
                    hashlib.sha256(f"{base_seed}:{operation}:{round_index}".encode()).digest()[:8], "big"
                )
                started = time.monotonic()
                found = len(self.findings)
                self.run_chunk(operation, examples, chunk_seed, deadline)
                done[operation] += examples
                if self.verbose:
                    print(
                        f"  {operation} round {round_index}: {time.monotonic() - started:.1f}s"
                        f"{' (new divergence)' if len(self.findings) > found else ''}",
                        file=sys.stderr,
                        flush=True,
                    )
            round_index += 1


REFERENCE_CRASH_NOTE = "Fix the reference adapter or mppx, or allowlist it with kind reference_crash."


def render_finding(divergence: Divergence) -> str:
    vector_file = VECTOR_FILES[divergence.operation]
    lines = [
        f"✗ {divergence.describe()}",
        f"  input:     {json.dumps(divergence.input, ensure_ascii=False)}",
        f"  {REFERENCE + ':':<10} {json.dumps(divergence.reference, ensure_ascii=False)}",
        f"  {divergence.adapter + ':':<10} {json.dumps(divergence.actual, ensure_ascii=False)}",
        f"  vector scenario for vectors/{vector_file}:",
    ]
    scenario = vector_scenario(divergence)
    if scenario is None:
        lines[-1] = f"  no vector scenario: {REFERENCE} crashed, so there is no result to expect. {REFERENCE_CRASH_NOTE}"
    else:
        lines.extend("    " + line for line in json.dumps(scenario, indent=2, ensure_ascii=False).splitlines())
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Differential fuzzing of conformance adapters against mppx.")
    parser.add_argument("--adapters", help="Comma-separated adapters to compare (default: all)")
    parser.add_argument("--operations", help=f"Comma-separated operations (default: {','.join(OPERATIONS)})")
    parser.add_argument("--budget", type=float, help="Stop after this many seconds (default: 60 without --max-examples)")
    parser.add_argument("--max-examples", type=int, help="Examples per operation")
    parser.add_argument("--seed", type=int, help="Seed for reproducible runs (default: random)")
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST_PATH, help="Known-differences file")
    parser.add_argument("--report", type=Path, help="Write a JSON report to this path")
    parser.add_argument("--build", action="store_true", help="Run adapter build commands first")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log each batch to stderr")
    args = parser.parse_args(argv)
    if args.budget is None and args.max_examples is None:
        args.budget = 60.0
    return args


def main(argv: list[str] | None = None) -> int:
    from harness import build_adapter

    args = parse_args(argv)
    discovered = discover_adapters()
    names = args.adapters.split(",") if args.adapters else sorted(discovered)
    unknown = sorted(set(names) - set(discovered))
    if unknown:
        print(f"Unknown adapters: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if REFERENCE not in names:
        names.append(REFERENCE)
    operations = args.operations.split(",") if args.operations else list(OPERATIONS)
    unknown = sorted(set(operations) - set(OPERATIONS))
    if unknown:
        print(f"Unsupported operations: {', '.join(unknown)}", file=sys.stderr)
        return 2
    allowlist = load_allowlist(args.allowlist)
    base_seed = args.seed if args.seed is not None else secrets.randbelow(2**32)

    adapters: dict[str, AdapterConfig] = {}
    build_errors: dict[str, str] = {}
    for name in names:
        error = build_adapter(discovered[name]) if args.build else None
        if not error:
            # An adapter that cannot answer a trivial request would read as a
            # crash on every case; report it once instead.
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

    print(f"diff-fuzz seed={base_seed} adapters={','.join(sorted(adapters))}", flush=True)
    fuzzer = Fuzzer(adapters, allowlist, verbose=args.verbose)
    started = time.monotonic()
    try:
        fuzzer.run(operations, budget=args.budget, max_examples=args.max_examples, base_seed=base_seed)
    finally:
        fuzzer.close()
    elapsed = time.monotonic() - started

    print(f"\nCases ({elapsed:.0f}s): " + ", ".join(f"{op}={fuzzer.cases[op]}" for op in sorted(fuzzer.cases)))
    if fuzzer.allowlisted:
        print("Allowlisted: " + ", ".join(f"{key}={count}" for key, count in sorted(fuzzer.allowlisted.items())))
    for finding in fuzzer.findings:
        print("\n" + render_finding(finding))
    print(f"\n{len(fuzzer.findings)} new divergence(s). Reproduce with --seed {base_seed}.")

    if args.report:
        args.report.write_text(json.dumps({
            "seed": base_seed,
            "adapters": sorted(adapters),
            "buildErrors": build_errors,
            "elapsedSeconds": round(elapsed, 1),
            "cases": dict(fuzzer.cases),
            "allowlisted": dict(fuzzer.allowlisted),
            "divergences": [
                {
                    "operation": d.operation,
                    "adapter": d.adapter,
                    "kind": d.kind,
                    "fields": list(d.fields),
                    "input": d.input,
                    "reference": d.reference,
                    "actual": d.actual,
                    "vectorFile": VECTOR_FILES[d.operation],
                    "scenario": vector_scenario(d),
                    **({"note": REFERENCE_CRASH_NOTE} if d.kind == "reference_crash" else {}),
                }
                for d in fuzzer.findings
            ],
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 1 if fuzzer.findings or build_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

