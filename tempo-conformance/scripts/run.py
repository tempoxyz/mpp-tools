#!/usr/bin/env python3
"""Tempo SDK conformance runner.

Runs every vector through every adapter and compares outputs exactly. Then, for
each adapter, it starts a fresh local Tempo dev node and submits the bytes that
adapter produced for every node case, checking the node's verdict, hash, sender
and fee payer. Standard library only. See ../SPEC.md.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = os.environ.get("TEMPO_IMAGE", "ghcr.io/tempoxyz/tempo:1.17.0")
FAUCET_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PATH_USD = "0x20c0000000000000000000000000000000000000"
CALL_TIMEOUT = float(os.environ.get("TEMPO_CONFORMANCE_CALL_TIMEOUT", "60"))


# --------------------------------------------------------------------------- adapters
class Adapter:
    def __init__(self, manifest_path: Path):
        self.dir = manifest_path.parent
        self.manifest = json.loads(manifest_path.read_text())
        self.name = self.manifest["name"]
        self.cwd = self.dir / self.manifest.get("cwd", ".")
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()

    def build(self) -> None:
        if self.manifest.get("build"):
            print(f"→ building {self.name}: {' '.join(self.manifest['build'])}", file=sys.stderr)
            subprocess.run(self.manifest["build"], cwd=self.cwd, check=True)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.manifest["command"], cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, bufsize=1,
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
        if op not in self.manifest.get("capabilities", []):
            return {"ok": False, "error": {"type": "unsupported", "message": f"{self.name} does not declare {op}"}}
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


def compare(case: dict, res: dict) -> tuple[str, str, dict]:
    """Returns (status, detail, signature). status is pass, fail or unsupported.

    signature identifies the kind of failure, so known-issues.json entries only
    cover the exact mismatch they document.
    """
    if not res.get("ok") and res.get("error", {}).get("type") == "unsupported":
        return "unsupported", res["error"].get("message", ""), {"unsupported": True}
    if "expectedError" in case:
        if res.get("ok"):
            return "fail", f"expected {case['expectedError']}, got ok: {json.dumps(res['value'])[:300]}", {"unexpectedOk": True}
        if res["error"]["type"] != case["expectedError"]:
            return "fail", f"expected {case['expectedError']}, got {res['error']['type']}: {res['error'].get('message', '')[:300]}", {"error": res["error"]["type"]}
        return "pass", "", {}
    if not res.get("ok"):
        return "fail", f"{res['error']['type']}: {res['error'].get('message', '')[:300]}", {"error": res["error"]["type"]}
    diffs = diff(case["expected"], res["value"])
    if not diffs:
        return "pass", "", {}
    return "fail", "; ".join(d for _, d in diffs[:6]), {"fields": sorted({p for p, _ in diffs})}


def diff(want, got, path="") -> list[tuple[str, str]]:
    if isinstance(want, dict) and isinstance(got, dict):
        out = []
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append((f"{path}.{k}", f"{path}.{k}: missing"))
            elif k not in want:
                out.append((f"{path}.{k}", f"{path}.{k}: unexpected key"))
            else:
                out += diff(want[k], got[k], f"{path}.{k}")
        return out
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            return [(path, f"{path}: length {len(got)} != {len(want)}")]
        return [d for i, (a, b) in enumerate(zip(want, got)) for d in diff(a, b, f"{path}[{i}]")]
    return [] if want == got else [(path, f"{path}: got {json.dumps(got)[:140]} want {json.dumps(want)[:140]}")]


def apply_known(status: str, detail: str, got: object, entry: dict | None, want_key: str) -> tuple[str, str]:
    """Downgrade a failure to KNOWN only when it matches the documented one."""
    if entry is None:
        return status, detail
    if status == "pass":
        return "fail", f"passes but is listed in known-issues.json ({entry['reason']}): remove the entry"
    if got == entry[want_key]:
        return "known", f"{detail} (known issue: {entry['reason']})"
    return status, f"{detail} (differs from the known issue, which expects {json.dumps(entry[want_key])})"


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
            subprocess.run(["docker", "rm", "-f", self.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.container = None


def run_node_case(node: Node, case: dict, raw: str, own_hash: str | None) -> tuple[str, list[str], dict]:
    """Submits raw; returns (verdict detail, problems, info). problems is a list
    of tags (verdict, hash, sender, feePayer, storedRaw, noReceipt, funding)."""
    want = case["node"]["expect"]
    info: dict = {}
    try:
        node.fund(case["node"].get("fund", []))
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
    exp = case.get("expected", {})
    for label, h in (("expected", exp.get("hash")), ("adapter", own_hash)):
        if h and tx_hash.lower() != h.lower():
            if "hash" not in problems:
                problems.append("hash")
            notes.append(f"node hash {tx_hash} != {label} hash {h}")
    if exp.get("sender") and (rc.get("from") or "").lower() != exp["sender"]:
        problems.append("sender")
        notes.append(f"receipt from {rc.get('from')} != expected sender {exp['sender']}")
    fee_payer = case["node"].get("feePayer")
    if fee_payer and (rc.get("feePayer") or "").lower() != fee_payer:
        problems.append("feePayer")
        notes.append(f"receipt feePayer {rc.get('feePayer')} != expected {fee_payer}")
    stored = node.rpc("eth_getRawTransactionByHash", [tx_hash])
    if stored and node.rpc("web3_sha3", [stored]).lower() != tx_hash.lower():
        problems.append("storedRaw")
        notes.append("eth_getRawTransactionByHash returns bytes whose keccak256 is not the transaction hash")
    return ("; ".join(notes) or got), problems, info


def node_stage(adapter: str, cases: list[dict], outputs: dict, vector_rows: dict, node_known: dict, url: str | None, image: str) -> tuple[dict, bool]:
    """Submits this adapter's bytes for every node case to one fresh node."""
    node = Node(url, image)
    rows: dict = {}
    failed = False
    try:
        print(f"\n== node for {adapter} ({node.version})")
        for c in cases:
            if "node" not in c:
                continue
            missing = [r for r in c["node"].get("requires", []) if rows.get(r, {}).get("status") not in ("pass", "known")]
            res = outputs.get(c["name"])
            if missing:
                rows[c["name"]] = {"status": "skipped", "detail": f"requires {', '.join(missing)}"}
            elif c["op"] == "tx.sign" and not (res and res.get("ok")):
                rows[c["name"]] = {"status": "skipped", "detail": f"{adapter} produced no transaction ({vector_rows[c['name']]['status']})"}
            else:
                raw = res["value"]["raw"] if c["op"] == "tx.sign" else c["input"]["raw"]
                own = res["value"].get("hash") if c["op"] == "tx.sign" else None
                detail, problems, info = run_node_case(node, c, raw, own)
                status = "fail" if problems else "pass"
                entry = node_known.get(c["name"])
                if entry is None and c["node"].get("knownIssue"):
                    entry = c["node"]["knownIssue"]
                status, detail = apply_known(status, detail, sorted(problems), {**entry, "problems": sorted(entry["problems"])} if entry else None, "problems")
                rows[c["name"]] = {"status": status, "detail": detail, "problems": problems, **info}
            failed |= rows[c["name"]]["status"] == "fail"
            r = rows[c["name"]]
            print(f"  {r['status'].upper():7} {c['name']}: {r['detail']}")
    finally:
        node.stop()
    return {"version": node.version, "cases": rows}, failed


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adapters", help="comma-separated adapter names (default: all)")
    ap.add_argument("--build", action="store_true", help="run each adapter's build first")
    ap.add_argument("--no-node", action="store_true", help="skip node submission")
    ap.add_argument("--rpc", default=os.environ.get("TEMPO_RPC_URL"), help="use this fresh node instead of starting one (one adapter only)")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--filter", help="only cases whose name contains this")
    args = ap.parse_args()

    adapters = [Adapter(p) for p in sorted((ROOT / "adapters").glob("*/adapter.json"))]
    if args.adapters:
        wanted = [n for n in args.adapters.split(",") if n]
        unknown = sorted(set(wanted) - {a.name for a in adapters})
        if unknown:
            print(f"unknown adapter(s): {', '.join(unknown)}; have {', '.join(a.name for a in adapters)}", file=sys.stderr)
            return 2
        adapters = [a for a in adapters if a.name in wanted]
    if args.rpc and len(adapters) > 1 and not args.no_node:
        print("--rpc needs a fresh node per adapter: pass one adapter with --adapters", file=sys.stderr)
        return 2
    known = json.loads((ROOT / "known-issues.json").read_text()) if (ROOT / "known-issues.json").exists() else {}
    vec_known, node_known = known.get("vectors", {}), known.get("node", {})

    files = sorted((ROOT / "vectors").glob("*.json"))
    cases = [c for f in files for c in json.loads(f.read_text())["cases"]]
    if args.filter:
        cases = [c for c in cases if args.filter in c["name"]]

    report: dict = {"adapters": {}, "node": {}}
    failed = False
    for a in adapters:
        if args.build:
            a.build()
        a.start()
        rows, outputs = {}, {}
        try:
            for c in cases:
                res = a.call(c["op"], c["input"])
                outputs[c["name"]] = res
                status, detail, sig = compare(c, res)
                status, detail = apply_known(status, detail, sig, vec_known.get(a.name, {}).get(c["name"]), "expect")
                rows[c["name"]] = {"status": status, "detail": detail, "signature": sig}
                failed |= status in ("fail", "unsupported")
        finally:
            a.stop()
        report["adapters"][a.name] = rows
        counts = {s: sum(1 for r in rows.values() if r["status"] == s) for s in ("pass", "fail", "unsupported", "known")}
        print(f"\n== {a.name}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        for n, r in rows.items():
            if r["status"] != "pass":
                print(f"  {r['status'].upper():11} {n}: {r['detail']}")

        if not args.no_node:
            node_report, node_failed = node_stage(a.name, cases, outputs, rows, node_known.get(a.name, {}), args.rpc, args.image)
            report["node"][a.name] = node_report
            failed |= node_failed

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

