#!/usr/bin/env python3
"""Tempo SDK conformance runner.

Runs every vector through every adapter, compares outputs exactly, then
submits each node case to a fresh local Tempo dev node and checks its verdict.
Standard library only. See ../SPEC.md.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = os.environ.get("TEMPO_IMAGE", "ghcr.io/tempoxyz/tempo:1.17.0")
FAUCET_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PATH_USD = "0x20c0000000000000000000000000000000000000"


# --------------------------------------------------------------------------- adapters
class Adapter:
    def __init__(self, manifest_path: Path):
        self.dir = manifest_path.parent
        self.manifest = json.loads(manifest_path.read_text())
        self.name = self.manifest["name"]
        self.cwd = self.dir / self.manifest.get("cwd", ".")
        self.proc: subprocess.Popen | None = None

    def build(self) -> None:
        if self.manifest.get("build"):
            print(f"→ building {self.name}: {' '.join(self.manifest['build'])}", file=sys.stderr)
            subprocess.run(self.manifest["build"], cwd=self.cwd, check=True)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.manifest["command"], cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, bufsize=1,
        )

    def call(self, op: str, payload: dict) -> dict:
        assert self.proc and self.proc.stdin and self.proc.stdout
        if op not in self.manifest.get("capabilities", []):
            return {"ok": False, "error": {"type": "unsupported", "message": f"{self.name} does not declare {op}"}}
        try:
            self.proc.stdin.write(json.dumps({"op": op, "input": payload}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except BrokenPipeError:
            line = ""
        if not line:
            # The adapter died: restart it so later cases still run.
            self.stop()
            self.start()
            return {"ok": False, "error": {"type": "crash", "message": "adapter exited without a response"}}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": {"type": "crash", "message": "non-JSON response: " + line[:200]}}

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            self.proc = None


def compare(case: dict, res: dict) -> tuple[str, str]:
    """Returns (status, detail): status is pass, fail or unsupported."""
    if not res.get("ok") and res.get("error", {}).get("type") == "unsupported":
        return "unsupported", res["error"].get("message", "")
    if "expectedError" in case:
        if res.get("ok"):
            return "fail", f"expected {case['expectedError']}, got ok: {json.dumps(res['value'])[:300]}"
        if res["error"]["type"] != case["expectedError"]:
            return "fail", f"expected {case['expectedError']}, got {res['error']['type']}: {res['error'].get('message', '')[:300]}"
        return "pass", ""
    if not res.get("ok"):
        return "fail", f"{res['error']['type']}: {res['error'].get('message', '')[:300]}"
    diffs = diff(case["expected"], res["value"])
    return ("pass", "") if not diffs else ("fail", "; ".join(diffs[:6]))


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
        hashes = []
        for a in addresses:
            hashes += self.rpc("tempo_fundAddress", [a]) or []
        for h in hashes:
            self.receipt(h)

    def stop(self) -> None:
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RpcError(Exception):
    pass


def keccak_hex(raw: str) -> str:
    # The node computes the hash; we only need keccak of returned raw for the
    # round-trip check, so ask the node's web3_sha3.
    return NODE.rpc("web3_sha3", [raw])  # type: ignore[name-defined]


def run_node_case(node: Node, case: dict) -> tuple[str, str, dict]:
    raw = case["expected"]["raw"] if "expected" in case and "raw" in case["expected"] else case["input"]["raw"]
    want = case["node"]["expect"]
    node.fund(case["node"].get("fund", []))
    info: dict = {}
    try:
        tx_hash = node.rpc("eth_sendRawTransaction", [raw])
    except RpcError as e:
        info["error"] = str(e)
        got = "reject"
        return ("pass" if want == "reject" else "fail"), f"rejected: {e}", info
    rc = node.receipt(tx_hash)
    if rc is None:
        return "fail", f"accepted as {tx_hash} but no receipt within 30 s", info
    got = "success" if rc["status"] == "0x1" else "revert"
    info.update({"hash": tx_hash, "status": rc["status"], "from": rc.get("from"), "feePayer": rc.get("feePayer"), "gasUsed": rc.get("gasUsed")})
    problems = []
    if got != want:
        problems.append(f"node verdict {got}, want {want}")
    exp = case.get("expected", {})
    if exp.get("hash") and tx_hash.lower() != exp["hash"]:
        problems.append(f"node hash {tx_hash} != expected {exp['hash']}")
    if exp.get("sender") and (rc.get("from") or "").lower() != exp["sender"]:
        problems.append(f"receipt from {rc.get('from')} != expected sender {exp['sender']}")
    if exp.get("feePayer") and (rc.get("feePayer") or "").lower() != exp["feePayer"]:
        problems.append(f"receipt feePayer {rc.get('feePayer')} != expected {exp['feePayer']}")
    stored = node.rpc("eth_getRawTransactionByHash", [tx_hash])
    if stored and node.rpc("web3_sha3", [stored]).lower() != tx_hash.lower():
        problems.append("eth_getRawTransactionByHash returns bytes whose keccak256 is not the transaction hash")
    if not problems:
        return "pass", got, info
    if case["node"].get("knownIssue"):
        return "known", "; ".join(problems) + f" (known issue: {case['node']['knownIssue']})", info
    return "fail", "; ".join(problems), info


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adapters", help="comma-separated adapter names (default: all)")
    ap.add_argument("--build", action="store_true", help="run each adapter's build first")
    ap.add_argument("--no-node", action="store_true", help="skip node submission")
    ap.add_argument("--rpc", default=os.environ.get("TEMPO_RPC_URL"), help="use this fresh node instead of starting one")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--filter", help="only cases whose name contains this")
    args = ap.parse_args()

    adapters = [Adapter(p) for p in sorted((ROOT / "adapters").glob("*/adapter.json"))]
    if args.adapters:
        wanted = set(args.adapters.split(","))
        adapters = [a for a in adapters if a.name in wanted]
    known = json.loads((ROOT / "known-issues.json").read_text()) if (ROOT / "known-issues.json").exists() else {}
    known.pop("_comment", None)

    files = sorted((ROOT / "vectors").glob("*.json"))
    cases = [(f.stem, c) for f in files for c in json.loads(f.read_text())["cases"]]
    if args.filter:
        cases = [(f, c) for f, c in cases if args.filter in c["name"]]

    report: dict = {"adapters": {}, "node": {}}
    failed = False
    for a in adapters:
        if args.build:
            a.build()
        a.start()
        rows = {}
        for _, c in cases:
            status, detail = compare(c, a.call(c["op"], c["input"]))
            reason = known.get(a.name, {}).get(c["name"])
            if status != "pass" and reason:
                status, detail = "known", f"{detail} (known issue: {reason})"
            elif status == "pass" and reason:
                status, detail = "fail", f"passes but is listed in known-issues.json ({reason}): remove the entry"
            rows[c["name"]] = {"status": status, "detail": detail}
            failed |= status in ("fail", "unsupported")
        a.stop()
        report["adapters"][a.name] = rows
        counts = {s: sum(1 for r in rows.values() if r["status"] == s) for s in ("pass", "fail", "unsupported", "known")}
        print(f"\n== {a.name}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        for n, r in rows.items():
            if r["status"] != "pass":
                print(f"  {r['status'].upper():11} {n}: {r['detail']}")

    if not args.no_node:
        global NODE
        NODE = node = Node(args.rpc, args.image)
        try:
            print(f"\n== node ({node.version}, {node.url})")
            rows = {}
            for _, c in cases:
                if "node" not in c:
                    continue
                status, detail, info = run_node_case(node, c)
                rows[c["name"]] = {"status": status, "detail": detail, **info}
                failed |= status == "fail"
                print(f"  {status.upper():6} {c['name']}: {detail}")
            report["node"] = {"version": node.version, "cases": rows}
        finally:
            node.stop()

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

