#!/usr/bin/env python3
"""Client-server pair conformance runner.

Starts each server adapter's http.server for a scenario, drives it with a
client adapter (its payment-aware HTTP client, plus its challenge.parse,
challenge.id and credential.format for hand-built credentials), and compares
normalized wire snapshots of every step with pairs/snapshots.json, which only
the TypeScript client and server regenerate (--update).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepdiff import DeepDiff

from harness import (
    AdapterClient,
    AdapterConfig,
    build_adapter,
    discover_adapters,
    load_json,
    stop_adapter_process,
    validate_value,
)


SCRIPT_DIR = Path(__file__).parent
CONFORMANCE_DIR = SCRIPT_DIR.parent
PAIRS_DIR = CONFORMANCE_DIR / "pairs"
SCENARIOS_PATH = PAIRS_DIR / "scenarios.json"
SNAPSHOTS_PATH = PAIRS_DIR / "snapshots.json"

REFERENCE = "typescript"
SERVER_CAPABILITY = "http.server"
CLIENT_CAPABILITIES = ("http.payment_request", "challenge.parse", "challenge.id", "credential.format")
SECRET_KEY = "pair-conformance-secret-key-32-bytes-minimum"
DEFAULT_SIGNATURE = "0xabc123"
MASK = "<masked>"
# Adapter calls start a fresh SDK process each; leave room for loaded CI runners.
ADAPTER_TIMEOUT = 60

# SDK-reserved challenge extensions that are not compared: per-issuance nonces
# in opaque, and pympp's route scope in the request. Their effect, rejecting
# credentials outside the route they were issued for, is what the scenarios check.
RESERVED_OPAQUE_KEYS = frozenset({"_mpp_nonce", "_mppx_nonce"})
RESERVED_REQUEST_KEYS = frozenset({"_mppx_scope"})


class ScenarioSkipped(Exception):
    """The server adapter cannot run this scenario, such as an SDK too old for http.server."""


@dataclass
class PairResult:
    client: str
    server: str
    scenario: str
    status: str  # pass, fail, xfail, xpass, skip
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in {"fail", "xpass"}


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    document = load_json(path)
    validate_value(document, "pair-scenarios.schema.json", str(path))
    names: set[str] = set()
    for scenario in document["scenarios"]:
        name = scenario["name"]
        if name in names:
            raise ValueError(f"{path} contains duplicate pair scenario name: {name}")
        names.add(name)
        seen: set[str] = set()
        for step in scenario["steps"]:
            for reference in (step.get("replay"), step.get("credential", {}).get("from")):
                if reference is not None and reference not in seen:
                    raise ValueError(f"{path} scenario {name} step {step['name']} refers to {reference}, not an earlier step")
            if step["name"] in seen:
                raise ValueError(f"{path} scenario {name} contains duplicate step name: {step['name']}")
            seen.add(step["name"])
    return document["scenarios"]


def load_snapshots(path: Path = SNAPSHOTS_PATH) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        raise RuntimeError(f"Missing pair snapshots: {path}. Run scripts/pair_runner.py --update to create them.")
    document = load_json(path)
    validate_value(document, "pair-snapshots.schema.json", str(path))
    return document["scenarios"]


def write_snapshots(snapshots: dict[str, list[dict[str, Any]]], path: Path = SNAPSHOTS_PATH) -> None:
    document = {"$schema": "../schemas/pair-snapshots.schema.json", "scenarios": snapshots}
    validate_value(document, "pair-snapshots.schema.json", str(path))
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def b64url_json(value: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


# auth-param: token "=" ( token / quoted-string ), RFC 9110 section 11.2.
AUTH_PARAM = re.compile(r'\s*([A-Za-z0-9_.-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]*)\s*')
SCHEME = re.compile(r"\s*Payment(?:\s+|$)", re.IGNORECASE)


def split_challenges(header: str) -> list[dict[str, str]]:
    """Parse the Payment challenges of one WWW-Authenticate value.

    A value may hold several challenges, such as a framework joining repeated
    headers with commas. Challenges of other schemes are skipped.
    """
    challenges: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    position = 0
    while position < len(header):
        scheme = SCHEME.match(header, position)
        if scheme:
            current = {}
            challenges.append(current)
            position = scheme.end()
            continue
        param = AUTH_PARAM.match(header, position)
        if param and param.group(0).strip():
            value = param.group(2)
            if value.startswith('"'):
                value = re.sub(r"\\(.)", r"\1", value[1:-1])
            if current is not None:
                current[param.group(1).lower()] = value
            position = param.end()
        else:
            # Another scheme's token, or stray text: skip to the next comma.
            current = None
            next_comma = header.find(",", position)
            position = len(header) if next_comma < 0 else next_comma
        if header.startswith(",", position):
            position += 1
    return challenges


def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def normalize_challenge(params: dict[str, str]) -> dict[str, Any]:
    challenge: dict[str, Any] = dict(params)
    if "id" in challenge:
        challenge["id"] = MASK
    if "request" in challenge:
        try:
            request = b64url_json(challenge["request"])
            if isinstance(request, dict):
                request = {key: value for key, value in request.items() if key not in RESERVED_REQUEST_KEYS}
            challenge["request"] = request
        except (binascii.Error, ValueError):
            pass
    if "opaque" in challenge:
        try:
            opaque = b64url_json(challenge["opaque"])
            if isinstance(opaque, dict):
                opaque = {key: value for key, value in opaque.items() if key not in RESERVED_OPAQUE_KEYS}
                if not opaque:
                    del challenge["opaque"]
                    return challenge
            challenge["opaque"] = opaque
        except (binascii.Error, ValueError):
            pass
    return challenge


def header_values(headers: Any, name: str) -> list[str]:
    """All values of a header from an http.client message, a list of pairs or a dict."""
    if hasattr(headers, "get_all"):
        return list(headers.get_all(name) or [])
    items = headers.items() if isinstance(headers, dict) else headers
    return [str(value) for key, value in items if str(key).lower() == name.lower()]


def normalize_response(status: int, headers: Any, body: bytes | str) -> dict[str, Any]:
    """The snapshot of one HTTP response, with volatile values masked."""
    snapshot: dict[str, Any] = {"status": status}
    challenges = [
        normalize_challenge(challenge)
        for value in header_values(headers, "WWW-Authenticate")
        for challenge in split_challenges(value)
    ]
    if challenges:
        snapshot["challenges"] = challenges
    receipts = header_values(headers, "Payment-Receipt")
    if receipts:
        try:
            receipt = b64url_json(receipts[0])
        except (binascii.Error, ValueError):
            receipt = {"unparsed": MASK}
        if isinstance(receipt, dict) and "challengeId" in receipt:
            receipt["challengeId"] = MASK
        snapshot["receipt"] = receipt
    cache_control = header_values(headers, "Cache-Control")
    if cache_control:
        snapshot["cacheControl"] = ", ".join(part.strip().lower() for part in ",".join(cache_control).split(",") if part.strip())
    content_type = header_values(headers, "Content-Type")
    media_type = content_type[0].split(";", 1)[0].strip().lower() if content_type else ""
    if media_type:
        snapshot["contentType"] = media_type
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    if media_type.endswith("json") and text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = MASK
        if media_type == "application/problem+json" and isinstance(parsed, dict):
            # title, detail and hints are prose; type and status are the protocol.
            parsed = {key: parsed[key] for key in ("type", "status") if key in parsed}
        snapshot["body"] = parsed
    return snapshot


def http_request(url: str, headers: dict[str, str]) -> tuple[int, Any, bytes]:
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def adapter_value(response: dict[str, Any], label: str) -> Any:
    if not response.get("ok"):
        error = response.get("error") or {}
        raise RuntimeError(f"{label} failed: {error.get('type')}: {error.get('message')}")
    return response["value"]


class ScenarioRun:
    """Runs one scenario's steps against a started server."""

    def __init__(self, client: AdapterClient, base_url: str, scenario: dict[str, Any]):
        self.client = client
        self.base_url = base_url
        self.scenario = scenario
        self.responses: dict[str, tuple[int, Any, bytes]] = {}
        self.sent: dict[str, dict[str, str]] = {}

    def run(self) -> list[dict[str, Any]]:
        return [{"name": step["name"], **self.run_step(step)} for step in self.scenario["steps"]]

    def run_step(self, step: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + step["path"]
        if step["kind"] == "pay":
            response = self.client.call(
                "http.payment_request",
                {
                    "url": url,
                    "method": "GET",
                    "headers": dict(step.get("headers", {})),
                    "body": None,
                    "payment": {"payload": {"type": "transaction", "signature": step.get("signature", DEFAULT_SIGNATURE)}},
                    "mode": "payment",
                },
                context={"caseName": f"{self.scenario['name']}.{step['name']}"},
                timeout=ADAPTER_TIMEOUT,
            )
            if not response.get("ok"):
                return {"error": (response.get("error") or {}).get("type", "unknown_error")}
            value = response["value"]
            return normalize_response(value["status"], value["headers"], value["body"])

        headers = dict(step.get("headers", {}))
        if "replay" in step:
            headers = dict(self.sent[step["replay"]])
        elif "authorization" in step:
            headers["Authorization"] = step["authorization"]
        elif "credential" in step:
            name, value = self.build_credential(step["credential"])
            headers[name] = value
        self.sent[step["name"]] = headers
        status, response_headers, body = http_request(url, headers)
        self.responses[step["name"]] = (status, response_headers, body)
        return normalize_response(status, response_headers, body)

    def build_credential(self, spec: dict[str, Any]) -> tuple[str, str]:
        _, headers, _ = self.responses[spec["from"]]
        raw = [
            challenge
            for value in header_values(headers, "WWW-Authenticate")
            for challenge in split_challenges(value)
        ]
        if spec.get("offer"):
            raw = [challenge for challenge in raw if challenge.get("method") == spec["offer"]]
        if not raw:
            raise RuntimeError(f"step {spec['from']} received no matching challenge")
        wire = "Payment " + ", ".join(f"{key}={quote(value)}" for key, value in raw[0].items())
        challenge = adapter_value(self.client.call("challenge.parse", {"header": wire}, timeout=ADAPTER_TIMEOUT), "challenge.parse")
        for key, value in (spec.get("mutate") or {}).items():
            if key == "request":
                challenge["request"] = {**challenge["request"], **value}
            else:
                challenge[key] = value
        if spec.get("rebind"):
            id_input = {
                "secretKey": SECRET_KEY,
                **{key: challenge[key] for key in ("realm", "method", "intent", "request", "expires", "digest", "header", "opaque") if key in challenge},
            }
            challenge["id"] = adapter_value(self.client.call("challenge.id", id_input, timeout=ADAPTER_TIMEOUT), "challenge.id")["id"]
        credential = {
            "challenge": challenge,
            "payload": {"type": "transaction", "signature": DEFAULT_SIGNATURE},
        }
        header = adapter_value(self.client.call("credential.format", credential, timeout=ADAPTER_TIMEOUT), "credential.format")["header"]
        return spec.get("header") or challenge.get("header") or "Authorization", header


class PortPool:
    """Hands out ports from base..base+99, or 0 (any free port) without a base."""

    def __init__(self, base: int | None):
        self.base = base
        self.next = 0
        self.lock = threading.Lock()

    def take(self) -> int:
        if self.base is None:
            return 0
        with self.lock:
            port = self.base + self.next % 100
            self.next += 1
        return port


def run_scenario(
    client: AdapterClient,
    server: AdapterClient,
    scenario: dict[str, Any],
    port: int,
) -> list[dict[str, Any]]:
    config = {
        "port": port,
        "realm": scenario["server"]["realm"],
        "secretKey": SECRET_KEY,
        "routes": scenario["server"]["routes"],
    }
    if scenario["server"].get("requiresAuth"):
        config["requiresAuth"] = True
    response, process = server.start(
        SERVER_CAPABILITY, config, context={"caseName": scenario["name"]}, timeout=ADAPTER_TIMEOUT
    )
    if process is None:
        error = response.get("error") or {}
        if error.get("type") == "unsupported_operation":
            raise ScenarioSkipped(str(error.get("message")))
        raise RuntimeError(f"{server.adapter.name} http.server failed: {error.get('type')}: {error.get('message')}")
    try:
        return ScenarioRun(client, response["value"]["url"], scenario).run()
    finally:
        stop_adapter_process(process)


def expected_failure(scenario: dict[str, Any], client: str, server: str) -> str | None:
    for entry in scenario.get("expectedFailures", []):
        if entry["client"] in {"*", client} and entry["server"] in {"*", server}:
            return entry["reason"]
    return None


def compare(
    scenario: dict[str, Any],
    expected: list[dict[str, Any]] | None,
    actual: list[dict[str, Any]] | Exception,
    client: str,
    server: str,
) -> PairResult:
    """Compare a run, or the error that stopped it, with the scenario's snapshot."""
    if isinstance(actual, Exception):
        diff = f"{type(actual).__name__}: {actual}"
    elif expected is None:
        diff = "missing snapshot; run --update"
    else:
        changes = DeepDiff(expected, actual, verbose_level=2)
        diff = json.dumps(changes, indent=2, default=str) if changes else ""
    reason = expected_failure(scenario, client, server)
    if reason is None:
        return PairResult(client, server, scenario["name"], "fail" if diff else "pass", diff or None)
    if diff:
        return PairResult(client, server, scenario["name"], "xfail", reason)
    return PairResult(client, server, scenario["name"], "xpass", f"expected failure no longer fails: {reason}")


def select_pairs(adapters: dict[str, AdapterConfig], adapter: str, client: str, server: str) -> list[tuple[str, str]]:
    clients = sorted(name for name, config in adapters.items() if all(op in config.capabilities for op in CLIENT_CAPABILITIES))
    servers = sorted(name for name, config in adapters.items() if SERVER_CAPABILITY in config.capabilities)
    if client != "all":
        clients = [name for name in clients if name == client]
    if server != "all":
        servers = [name for name in servers if name == server]
    pairs = [(c, s) for c in clients for s in servers]
    if adapter != "all":
        # The adapter under test against the reference, both ways, and itself.
        pairs = [(c, s) for c, s in pairs if adapter in {c, s} and {c, s} <= {adapter, REFERENCE}]
    return pairs


def format_matrix(results: list[PairResult]) -> str:
    clients = sorted({result.client for result in results})
    servers = sorted({result.server for result in results})
    cells: dict[tuple[str, str], str] = {}
    for c in clients:
        for s in servers:
            pair = [result for result in results if result.client == c and result.server == s]
            if not pair:
                cells[c, s] = "-"
                continue
            counts = {status: sum(1 for result in pair if result.status == status) for status in ("pass", "fail", "xfail", "xpass", "skip")}
            if counts["skip"] == len(pair):
                cells[c, s] = "skip"
                continue
            ran = len(pair) - counts["skip"]
            cell = f"{counts['pass'] + counts['xfail']}/{ran}"
            notes = [f"{counts[status]} {status}" for status in ("fail", "xpass", "xfail", "skip") if counts[status]]
            cells[c, s] = cell + (f" ({', '.join(notes)})" if notes else "")
    width = max([len("client \\ server")] + [len(c) for c in clients])
    column = {s: max(len(s), *(len(cells[c, s]) for c in clients)) for s in servers}
    lines = ["client \\ server".ljust(width) + "  " + "  ".join(s.ljust(column[s]) for s in servers)]
    for c in clients:
        lines.append(c.ljust(width) + "  " + "  ".join(cells[c, s].ljust(column[s]) for s in servers))
    return "\n".join(line.rstrip() for line in lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run client-server pair conformance tests")
    parser.add_argument("--adapter", default="all", help="Pair this adapter with itself and the TypeScript reference")
    parser.add_argument("--client", default="all")
    parser.add_argument("--server", default="all")
    parser.add_argument("--scenario", action="append", help="Run only this scenario (repeatable)")
    parser.add_argument("--base-port", type=int, help="Bind servers to ports base..base+99 instead of any free port")
    parser.add_argument("--jobs", type=int, default=2, help="Pairs to run in parallel")
    parser.add_argument("--update", action="store_true", help="Regenerate pairs/snapshots.json from the TypeScript pair")
    args = parser.parse_args()

    adapters = discover_adapters()
    scenarios = load_scenarios()
    if args.scenario:
        unknown = set(args.scenario) - {scenario["name"] for scenario in scenarios}
        if unknown:
            print(f"Unknown scenario: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        scenarios = [scenario for scenario in scenarios if scenario["name"] in args.scenario]
    ports = PortPool(args.base_port)

    if args.update:
        reference = AdapterClient(adapters[REFERENCE])
        snapshots = load_snapshots() if args.scenario and SNAPSHOTS_PATH.exists() else {}
        for scenario in scenarios:
            print(f"Recording {scenario['name']}...", flush=True)
            snapshots[scenario["name"]] = run_scenario(reference, reference, scenario, ports.take())
        write_snapshots({name: snapshots[name] for name in sorted(snapshots)})
        print(f"Updated {SNAPSHOTS_PATH}")
        return 0

    snapshots = load_snapshots()
    for name in (args.adapter, args.client, args.server):
        if name != "all" and name not in adapters:
            print(f"Unknown adapter: {name}", file=sys.stderr)
            return 2
    pairs = select_pairs(adapters, args.adapter, args.client, args.server)
    if not pairs and args.adapter != "all":
        # Like server.verify, pairs are opt-in: an adapter without the client
        # or server capabilities has nothing to run.
        print(f"{args.adapter} declares neither {SERVER_CAPABILITY} nor the client operations; skipping pair conformance.")
        return 0
    build_errors = {name: build_adapter(adapters[name]) for name in sorted({name for pair in pairs for name in pair})}

    def run_pair(pair: tuple[str, str]) -> list[PairResult]:
        client_name, server_name = pair
        client, server = AdapterClient(adapters[client_name]), AdapterClient(adapters[server_name])
        build_error = build_errors[client_name] or build_errors[server_name]
        results: list[PairResult] = []
        for scenario in scenarios:
            if build_error:
                results.append(PairResult(client_name, server_name, scenario["name"], "fail", build_error))
                continue
            actual: list[dict[str, Any]] | Exception
            try:
                actual = run_scenario(client, server, scenario, ports.take())
            except ScenarioSkipped as exc:
                results.append(PairResult(client_name, server_name, scenario["name"], "skip", str(exc)))
                continue
            except Exception as exc:
                actual = exc
            results.append(compare(scenario, snapshots.get(scenario["name"]), actual, client_name, server_name))
        print(f"{client_name} client -> {server_name} server: {'failed' if any(r.failed for r in results) else 'ok'}", flush=True)
        return results

    results: list[PairResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for pair_results in pool.map(run_pair, pairs):
            results.extend(pair_results)

    print("")
    if results:
        print(format_matrix(results))
        print("")
    xfails = [result for result in results if result.status == "xfail"]
    if xfails:
        print("Expected failures:")
        for result in xfails:
            print(f"  {result.client} -> {result.server} {result.scenario}: {result.detail}")
        print("")
    skipped: dict[tuple[str, str, str], int] = {}
    for result in results:
        if result.status == "skip":
            key = (result.client, result.server, str(result.detail))
            skipped[key] = skipped.get(key, 0) + 1
    if skipped:
        print("Skipped:")
        for (client_name, server_name, detail), count in skipped.items():
            print(f"  {client_name} -> {server_name}: {count} scenario(s): {detail}")
        print("")
    failures = [result for result in results if result.failed]
    executed = [result for result in results if result.status != "skip"]
    if not executed:
        print("FAILED: no pair scenarios were executed")
        return 1
    if failures:
        print(f"FAILED: {len(failures)} of {len(executed)} pair scenarios")
        print("")
        for index, result in enumerate(failures, start=1):
            print(f"  {index}) {result.client} -> {result.server} {result.scenario} [{result.status}]")
            for line in (result.detail or "").split("\n"):
                print(f"       {line}")
        return 1
    print(f"PASSED: {len(executed)} pair scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())

