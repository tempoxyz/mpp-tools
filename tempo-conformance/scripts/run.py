#!/usr/bin/env python3
"""Tempo SDK conformance runner.

Runs every scenario in vectors/*.json (the MPP vector format: one operation per
file, scenarios with input and expected) through every adapter and compares the
responses exactly. A scenario is skipped for an adapter, with its recorded
reason, when the adapter does not advertise the file's operation (capabilities
and capabilityGaps in adapter.json), when its adapters list leaves the adapter
out, or when the adapter's pinned SDK version does not satisfy its sdkVersions
constraint (evaluated with ../conformance/scripts/version_constraints.py, the
MPP suite's matcher, against the version scripts/sdk_version.py reads from the
adapter's lockfile).

Then, for each adapter, it starts a fresh local Tempo dev node and submits the
bytes that adapter produced for every scenario with node, checking the node's
verdict, hash, sender and fee payer. See ../SPEC.md.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path

import sdk_version
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent


def _mpp_module(name: str):
    """A module from the MPP suite's scripts (../conformance/scripts), loaded by path
    so it cannot be confused with this suite's own scripts of the same name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"mpp_{name}", ROOT.parent / "conformance" / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# The MPP suite's SemVer matcher, so sdkVersions means the same thing in both suites.
matches_constraint = _mpp_module("version_constraints").matches_constraint

DEFAULT_IMAGE = os.environ.get("TEMPO_IMAGE", "ghcr.io/tempoxyz/tempo:1.17.0")
FAUCET_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PATH_USD = "0x20c0000000000000000000000000000000000000"
CALL_TIMEOUT = float(os.environ.get("TEMPO_CONFORMANCE_CALL_TIMEOUT", "60"))


# --------------------------------------------------------------------------- schemas
def _load_schemas() -> tuple[Registry, str]:
    resources, base = [], ""
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        schema = json.loads(path.read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
        base = schema["$id"].rsplit("/", 1)[0] + "/"
    return Registry().with_resources(resources), base


SCHEMA_REGISTRY, SCHEMA_BASE = _load_schemas()


def validate(value, ref: str, label: str) -> None:
    """ref: a schema file name, or file#/pointer, relative to schemas/."""
    validator = Draft202012Validator({"$ref": SCHEMA_BASE + ref}, registry=SCHEMA_REGISTRY)
    error = next(iter(sorted(validator.iter_errors(value), key=lambda e: e.path)), None)
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path)
        raise ValueError(f"{label}{' at ' + where if where else ''}: {error.message[:300]}")


# --------------------------------------------------------------------------- adapters
def _adapter_env() -> dict[str, str]:
    """The adapters' own toolchains pick their environments: drop the runner's venv."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env


class Adapter:
    def __init__(self, manifest_path: Path, operations: dict):
        self.dir = manifest_path.parent
        self.manifest = json.loads(manifest_path.read_text())
        validate(self.manifest, "adapter-manifest.schema.json", str(manifest_path.relative_to(ROOT)))
        self.name = self.manifest["name"]
        if self.name != self.dir.name:
            raise ValueError(f"{manifest_path}: name {self.name!r} must match its directory")
        self.capabilities = set(self.manifest["capabilities"])
        self.gaps = self.manifest.get("capabilityGaps", {})
        for op in operations:
            if op not in self.capabilities and op not in self.gaps:
                raise ValueError(f"{self.name}: lacks {op} without a capabilityGaps reason")
        for op in self.gaps:
            if op in self.capabilities:
                raise ValueError(f"{self.name}: capabilityGaps lists {op}, which it advertises")
        self.cwd = self.dir / self.manifest.get("cwd", ".")
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self._version: str | None = None

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = sdk_version.installed_version(self.name)
        return self._version

    def build(self) -> None:
        if self.manifest.get("build"):
            print(f"→ building {self.name}: {' '.join(self.manifest['build'])}", file=sys.stderr)
            subprocess.run(self.manifest["build"], cwd=self.cwd, env=_adapter_env(), check=True)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.manifest["command"], cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, bufsize=1, env=_adapter_env(),
        )
        self.lines = queue.Queue()
        proc, lines = self.proc, self.lines

        def pump() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
            lines.put("")  # EOF

        threading.Thread(target=pump, daemon=True).start()

    def restart(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait()
        self.start()

    def call(self, op: str, payload: dict) -> dict:
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps({"op": op, "input": payload}) + "\n")
            self.proc.stdin.flush()
            line = self.lines.get(timeout=CALL_TIMEOUT)
        except BrokenPipeError:
            line = ""
        except queue.Empty:
            self.restart()
            return {"ok": False, "error": {"type": "crash", "message": f"no response within {CALL_TIMEOUT:.0f} s"}}
        if not line:
            self.restart()
            return {"ok": False, "error": {"type": "crash", "message": "adapter exited without a response"}}
        try:
            res = json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": {"type": "crash", "message": "non-JSON response: " + line[:200]}}
        ok = isinstance(res, dict) and (
            (res.get("ok") is True and "value" in res)
            or (res.get("ok") is False and isinstance(res.get("error"), dict) and isinstance(res["error"].get("type"), str))
        )
        if not ok:
            return {"ok": False, "error": {"type": "crash", "message": "malformed response: " + line[:200]}}
        return res

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            self.proc = None


# --------------------------------------------------------------------------- vectors
def load_vectors(operations: dict, adapter_names: set[str]) -> list[tuple[str, str, list[dict]]]:
    """Returns [(file stem, operation, scenarios)], validated."""
    out, seen, node_names = [], set(), set()
    for path in sorted((ROOT / "vectors").glob("*.json")):
        label = str(path.relative_to(ROOT))
        doc = json.loads(path.read_text())
        validate(doc, "vector.schema.json", label)
        op = doc["commands"]["operation"]
        if op not in operations:
            raise ValueError(f"{label}: operation {op} is not in operations.json")
        reg = operations[op]
        for s in doc["scenarios"]:
            where = f"{label} {s['name']}"
            if s["name"] in seen:
                raise ValueError(f"{where}: duplicate scenario name")
            seen.add(s["name"])
            validate(s["input"], reg["inputRef"], f"{where} input")
            if s["expected"]["ok"]:
                validate(s["expected"]["value"], reg["successRef"], f"{where} expected.value")
            elif s["expected"]["error"]["type"] not in reg["errorTypes"]:
                raise ValueError(f"{where}: error type {s['expected']['error']['type']} is not one of {op}'s errorTypes")
            check_skips(s, adapter_names, where)
            for r in s.get("node", {}).get("requires", []):
                if r not in node_names:
                    raise ValueError(f"{where}: node.requires {r}, which is not an earlier node scenario")
            if "node" in s:
                node_names.add(s["name"])
        out.append((path.stem, op, doc["scenarios"]))
    return out


def check_skips(s: dict, adapter_names: set[str], where: str) -> None:
    """Every skip carries its reason: skipReasons has exactly one entry per
    adapter that sdkVersions constrains or adapters leaves out."""
    versions = s.get("sdkVersions", {})
    only = s.get("adapters")
    reasons = s.get("skipReasons", {})
    for name in [*versions, *(only or []), *reasons]:
        if name not in adapter_names:
            raise ValueError(f"{where}: unknown adapter {name!r}")
    for name, expression in versions.items():
        try:
            matches_constraint("0.0.0", expression)
        except ValueError as e:
            raise ValueError(f"{where}: sdkVersions.{name}: {e}") from None
    need = set(versions) | (adapter_names - set(only) if only else set())
    if set(reasons) != need:
        missing, extra = sorted(need - set(reasons)), sorted(set(reasons) - need)
        raise ValueError(f"{where}: skipReasons must cover exactly the skipped adapters (missing {missing}, extra {extra})")


def skip_reason(s: dict, a: Adapter, op: str) -> tuple[str, str] | None:
    """(kind, detail) when the scenario does not run on this adapter."""
    if op not in a.capabilities:
        return "capability", f"{a.name} lacks {op}: {a.gaps[op]}"
    only = s.get("adapters")
    if only and a.name not in only:
        return "adapters", s["skipReasons"][a.name]
    expression = s.get("sdkVersions", {}).get(a.name)
    if expression and not matches_constraint(a.version, expression):
        return "sdkVersions", f"{a.name}@{a.version} does not satisfy {expression}: {s['skipReasons'][a.name]}"
    return None


def compare(expected: dict, res: dict) -> tuple[bool, str]:
    if expected["ok"] is False:
        want = expected["error"]["type"]
        if res.get("ok"):
            return False, f"expected {want}, got ok: {json.dumps(res['value'])[:300]}"
        got = res["error"]
        if got["type"] != want:
            return False, f"expected {want}, got {got['type']}: {str(got.get('message', ''))[:300]}"
        needle = expected["error"].get("messageContains")
        if needle is not None and needle not in str(got.get("message", "")):
            return False, f"error message does not contain {needle!r}: {str(got.get('message', ''))[:300]}"
        return True, ""
    if not res.get("ok"):
        return False, f"{res['error']['type']}: {str(res['error'].get('message', ''))[:300]}"
    diffs = diff(expected["value"], res["value"])
    return (not diffs), "; ".join(diffs[:6])


def diff(want, got, path="") -> list[str]:
    if isinstance(want, dict) and isinstance(got, dict):
        out = []
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append(f"{path}.{k}: missing")
            elif k not in want:
                out.append(f"{path}.{k}: unexpected key")
            else:
                out += diff(want[k], got[k], f"{path}.{k}")
        return out
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            return [f"{path}: length {len(got)} != {len(want)}"]
        return [d for i, (a, b) in enumerate(zip(want, got)) for d in diff(a, b, f"{path}[{i}]")]
    return [] if want == got else [f"{path}: got {json.dumps(got)[:140]} want {json.dumps(want)[:140]}"]


# --------------------------------------------------------------------------- node
class RpcError(Exception):
    pass


class Node:
    def __init__(self, url: str | None, image: str):
        self.container = None
        if url:
            self.url = url
        else:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            self.container = f"tempo-conformance-{port}"
            subprocess.run([
                "docker", "run", "-d", "--rm", "--name", self.container, "-p", f"127.0.0.1:{port}:8545", image,
                "node", "--dev", "--dev.block-time", "1sec", "--http", "--http.addr", "0.0.0.0", "--http.port", "8545",
                "--http.api", "all", "--chain", "dev", "--faucet.enabled", "--faucet.private-key", FAUCET_KEY,
                "--faucet.amount", "1000000000000000", "--faucet.address", PATH_USD,
            ], check=True, stdout=subprocess.DEVNULL)
            self.url = f"http://127.0.0.1:{port}"
        try:
            self._wait_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_ready(self) -> None:
        for _ in range(120):
            try:
                if self.rpc("eth_blockNumber", []):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Tempo node did not come up at " + self.url)
        # A fresh dev chain starts near timestamp 0; the faucet signs with an
        # expiring nonce bound to wall-clock time, so wait for blocks to catch up.
        for _ in range(240):
            block = self.rpc("eth_getBlockByNumber", ["latest", False])
            if block and int(block["timestamp"], 16) > time.time() - 60:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Tempo node block timestamps never reached wall-clock time")
        self.version = self.rpc("web3_clientVersion", [])

    def rpc(self, method: str, params: list):
        req = urllib.request.Request(self.url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                     headers={"content-type": "application/json"})
        body = json.load(urllib.request.urlopen(req, timeout=30))
        if "error" in body:
            raise RpcError(body["error"].get("message", str(body["error"])))
        return body["result"]

    def receipt(self, tx_hash: str, timeout: float = 30):
        end = time.time() + timeout
        while time.time() < end:
            r = self.rpc("eth_getTransactionReceipt", [tx_hash])
            if r:
                return r
            time.sleep(0.25)
        return None

    def fund(self, addresses: list[str]) -> None:
        """Faucet-fund each address; raise unless every funding tx succeeded."""
        for a in addresses:
            hashes = self.rpc("tempo_fundAddress", [a]) or []
            if not hashes:
                raise RuntimeError(f"faucet returned no transactions for {a}")
            for h in hashes:
                rc = self.receipt(h)
                if rc is None or rc.get("status") != "0x1":
                    raise RuntimeError(f"faucet transaction {h} for {a} {'never mined' if rc is None else 'reverted'}")

    def stop(self) -> None:
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.container = None


def run_node_case(node: Node, s: dict, raw: str, own_hash: str | None) -> tuple[str, list[str], dict]:
    """Submits raw; returns (verdict detail, problems, info). problems is a list
    of tags (verdict, hash, sender, feePayer, storedRaw, noReceipt, funding)."""
    want = s["node"]["expect"]
    info: dict = {}
    try:
        node.fund(s["node"].get("fund", []))
    except Exception as e:
        return f"funding failed: {e}", ["funding"], info
    try:
        tx_hash = node.rpc("eth_sendRawTransaction", [raw])
    except RpcError as e:
        info["error"] = str(e)
        return f"rejected: {e}", ([] if want == "reject" else ["verdict"]), info
    rc = node.receipt(tx_hash)
    if rc is None:
        return f"accepted as {tx_hash} but no receipt within 30 s", ["noReceipt"], info
    got = "success" if rc["status"] == "0x1" else "revert"
    info.update({"hash": tx_hash, "receiptStatus": rc["status"], "from": rc.get("from"), "feePayer": rc.get("feePayer"), "gasUsed": rc.get("gasUsed")})
    problems, notes = [], []
    if got != want:
        problems.append("verdict")
        notes.append(f"node verdict {got}, want {want}")
    exp = s["expected"].get("value") or {}
    for label, h in (("expected", exp.get("hash")), ("adapter", own_hash)):
        if h and tx_hash.lower() != h.lower():
            if "hash" not in problems:
                problems.append("hash")
            notes.append(f"node hash {tx_hash} != {label} hash {h}")
    if exp.get("sender") and (rc.get("from") or "").lower() != exp["sender"]:
        problems.append("sender")
        notes.append(f"receipt from {rc.get('from')} != expected sender {exp['sender']}")
    fee_payer = s["node"].get("feePayer")
    if fee_payer and (rc.get("feePayer") or "").lower() != fee_payer:
        problems.append("feePayer")
        notes.append(f"receipt feePayer {rc.get('feePayer')} != expected {fee_payer}")
    stored = node.rpc("eth_getRawTransactionByHash", [tx_hash])
    if stored and node.rpc("web3_sha3", [stored]).lower() != tx_hash.lower():
        problems.append("storedRaw")
        notes.append("eth_getRawTransactionByHash returns bytes whose keccak256 is not the transaction hash")
    return ("; ".join(notes) or got), problems, info


def apply_node_known(status: str, detail: str, problems: list[str], known: dict | None) -> tuple[str, str]:
    """A node knownIssue downgrades a failure to KNOWN only when its problems are
    exactly the listed ones; a listed case that passes fails, so fixes get unlisted."""
    if known is None:
        return status, detail
    if status == "pass":
        return "fail", f"passes but carries a node knownIssue ({known['reason']}): remove it"
    if sorted(problems) == sorted(known["problems"]):
        return "known", f"{detail} (known node issue: {known['reason']})"
    return status, f"{detail} (differs from the node knownIssue, which expects {sorted(known['problems'])})"


def node_stage(adapter: str, vectors: list, outputs: dict, rows: dict, url: str | None, image: str) -> tuple[dict, bool]:
    """Submits this adapter's bytes for every node scenario to one fresh node."""
    node = Node(url, image)
    node_rows: dict = {}
    failed = False
    try:
        print(f"\n== node for {adapter} ({node.version})")
        for _, op, scenarios in vectors:
            for s in scenarios:
                if "node" not in s or s["name"] not in rows:
                    continue
                name = s["name"]
                missing = [r for r in s["node"].get("requires", []) if node_rows.get(r, {}).get("status") not in ("pass", "known")]
                res = outputs.get(name)
                if missing:
                    row = {"status": "skipped", "detail": f"requires {', '.join(missing)}"}
                elif op == "tx.sign" and not (res and res.get("ok")):
                    row = {"status": "skipped", "detail": f"{adapter} produced no transaction ({rows[name]['status']}: {rows[name]['detail'][:200]})"}
                else:
                    raw = res["value"]["raw"] if op == "tx.sign" else s["input"]["raw"]
                    own = res["value"].get("hash") if op == "tx.sign" else None
                    detail, problems, info = run_node_case(node, s, raw, own)
                    status, detail = apply_node_known("fail" if problems else "pass", detail, problems, s["node"].get("knownIssue"))
                    row = {"status": status, "detail": detail, "problems": problems, **info}
                node_rows[name] = row
                failed |= row["status"] == "fail"
                print(f"  {row['status'].upper():7} {name}: {row['detail']}")
    finally:
        node.stop()
    counts = Counter(r["status"] for r in node_rows.values())
    print("  -> node: " + ", ".join(f"{counts.get(k, 0)} {k}" for k in ("pass", "fail", "known", "skipped")))
    return {"version": node.version, "summary": dict(counts), "scenarios": node_rows}, failed


# --------------------------------------------------------------------------- main
def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapters", help="comma-separated adapter names (default: all)")
    ap.add_argument("--build", action="store_true", help="run each adapter's build first")
    ap.add_argument("--no-node", action="store_true", help="skip node submission")
    ap.add_argument("--rpc", default=os.environ.get("TEMPO_RPC_URL"), help="use this fresh node instead of starting one (one adapter only)")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--filter", help="only scenarios whose name contains this")
    ap.add_argument("--tag", help="only scenarios with this tag")
    ap.add_argument("--list-skips", action="store_true", help="print every skipped scenario with its reason")
    ap.add_argument("--probe-skips", action="store_true",
                    help="also run sdkVersions/adapters-skipped scenarios and list those that already pass (informational)")
    ap.add_argument("--sdk-version", action="append", default=[], metavar="ADAPTER=VERSION",
                    help="evaluate sdkVersions as if ADAPTER's SDK were VERSION (when testing an unreleased SDK checkout)")
    ap.add_argument("--build-only", action="store_true", help="build the selected adapters, then exit")
    ap.add_argument("--validate-only", action="store_true", help="validate vectors, schemas and manifests, then exit")
    args = ap.parse_args()

    operations = json.loads((ROOT / "operations.json").read_text())
    validate(operations, "operation-registry.schema.json", "operations.json")
    operations = operations["operations"]
    adapters = [Adapter(p, operations) for p in sorted((ROOT / "adapters").glob("*/adapter.json"))]
    names = {a.name for a in adapters}
    vectors = load_vectors(operations, names)
    total = sum(len(s) for _, _, s in vectors)
    print(f"{total} scenarios in {len(vectors)} files, {len(operations)} operations, adapters: {', '.join(sorted(names))}")
    if args.validate_only:
        return 0

    if args.adapters:
        wanted = [n for n in args.adapters.split(",") if n]
        unknown = sorted(set(wanted) - names)
        if unknown:
            print(f"unknown adapter(s): {', '.join(unknown)}; have {', '.join(sorted(names))}", file=sys.stderr)
            return 2
        adapters = [a for a in adapters if a.name in wanted]
    for override in args.sdk_version:
        name, _, version = override.partition("=")
        if name not in names or not version:
            print(f"--sdk-version wants ADAPTER=VERSION with ADAPTER one of {', '.join(sorted(names))}", file=sys.stderr)
            return 2
        for a in adapters:
            if a.name == name:
                a._version = version
    if args.rpc and len(adapters) > 1 and not args.no_node:
        print("--rpc needs a fresh node per adapter: pass one adapter with --adapters", file=sys.stderr)
        return 2

    if args.build_only:
        for a in adapters:
            a.build()
        return 0

    def selected(s: dict) -> bool:
        return (not args.filter or args.filter in s["name"]) and (not args.tag or args.tag in s["tags"])

    report: dict = {"adapters": {}, "node": {}}
    failed = False
    executed = 0
    for a in adapters:
        if args.build:
            a.build()
        sdk = sdk_version.sdk_summary(a.name) if a._version is None else f"{a.manifest['sdk']['package']}@{a._version} (--sdk-version)"
        a.start()
        rows, outputs, probes = {}, {}, []
        try:
            for _, op, scenarios in vectors:
                for s in scenarios:
                    if not selected(s):
                        continue
                    skip = skip_reason(s, a, op)
                    if skip:
                        rows[s["name"]] = {"status": "skipped", "kind": skip[0], "detail": skip[1]}
                        if args.probe_skips and skip[0] != "capability":
                            ok, _ = compare(s["expected"], a.call(op, s["input"]))
                            if ok:
                                probes.append(s["name"])
                        continue
                    res = a.call(op, s["input"])
                    outputs[s["name"]] = res
                    ok, detail = compare(s["expected"], res)
                    rows[s["name"]] = {"status": "pass" if ok else "fail", "detail": detail}
                    executed += 1
                    failed |= not ok
        finally:
            a.stop()
        statuses = Counter(r["status"] for r in rows.values())
        kinds = Counter(r["kind"] for r in rows.values() if r["status"] == "skipped")
        summary = {"passed": statuses.get("pass", 0), "failed": statuses.get("fail", 0), "skipped": statuses.get("skipped", 0), "skippedBy": dict(kinds)}
        report["adapters"][a.name] = {"sdk": sdk, "summary": summary, "scenarios": rows, **({"probedSkipsPassing": probes} if args.probe_skips else {})}
        skipped = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        print(f"\n== {a.name} ({sdk}): {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped{f' ({skipped})' if skipped else ''}")
        for n, r in rows.items():
            if r["status"] == "fail":
                print(f"  FAIL    {n}: {r['detail']}")
        if args.list_skips:
            for n, r in rows.items():
                if r["status"] == "skipped":
                    print(f"  SKIP    {n} [{r['kind']}] {r['detail']}")
        else:
            reasons = Counter((r["kind"], r["detail"]) for r in rows.values() if r["status"] == "skipped")
            for (kind, detail), count in reasons.items():
                print(f"  SKIP x{count:<3} [{kind}] {detail[:240]}")
        if probes:
            print(f"  {len(probes)} skipped scenario(s) already pass on {sdk} (a skip may be stale): {', '.join(probes)}")

        if not args.no_node:
            node_report, node_failed = node_stage(a.name, vectors, outputs, rows, args.rpc, args.image)
            report["node"][a.name] = node_report
            failed |= node_failed

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    if executed == 0:
        print("no scenario ran", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

