#!/usr/bin/env python3
"""Flow conformance runner using the shared adapter ABI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepdiff import DeepDiff

from conformance_checks import make_check
from harness import AdapterClient, AdapterConfig, build_adapter, discover_adapters


SCRIPT_DIR = Path(__file__).parent
CONFORMANCE_DIR = SCRIPT_DIR.parent
FLOW_DIR = CONFORMANCE_DIR / "flows"
FLOW_CASES = FLOW_DIR / "flows.json"
FLOW_RESULTS = FLOW_DIR / "golden-results.json"


def normalize_error_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value.split(":", 1)[0].strip()


def normalize_result(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(entry))
    outcome = normalized.get("outcome")
    if isinstance(outcome, dict):
        outcome["error_type"] = normalize_error_type(outcome.get("error_type"))
        outcome.pop("content_type", None)
    challenge = normalized.get("challenge")
    if isinstance(challenge, dict):
        challenge.pop("expires", None)
    normalized.pop("problem_details", None)
    return normalized


def compute_diff(expected: Any, actual: Any) -> str:
    diff = DeepDiff(expected, actual, ignore_order=True, verbose_level=2)
    if not diff:
        return ""
    return json.dumps(diff, indent=2, default=str)


@dataclass
class RunResult:
    adapter: str
    name: str
    passed: bool
    error: str | None = None

    def to_check(self) -> dict[str, Any]:
        return make_check(
            id_parts=["flow", self.name, self.adapter],
            name=f"{self.adapter} flow {self.name}",
            description=f"End-to-end 402 payment flow conformance for {self.name}",
            passed=self.passed,
            spec_ref="draft-ietf-httpauth-payment",
            details={
                "adapter": self.adapter,
                "flow": self.name,
            },
            error=self.error,
        )


def start_server(
    cmd: list[str],
    env: dict[str, str],
    verbose: bool,
    output_format: str,
) -> subprocess.Popen[str]:
    output = sys.stderr if output_format == "json" else None
    return subprocess.Popen(
        cmd,
        stdout=output if verbose else subprocess.DEVNULL,
        stderr=output if verbose else subprocess.DEVNULL,
        text=True,
        cwd=CONFORMANCE_DIR,
        env=env,
    )


def wait_for_server(base_url: str, timeout: int = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/free", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("Server did not start")


def load_results(data: str) -> list[dict[str, Any]]:
    parsed = json.loads(data)
    results = parsed.get("results")
    if not isinstance(results, list):
        raise ValueError("Invalid results payload")
    return results


def load_flow_cases() -> list[dict[str, Any]]:
    parsed = json.loads(FLOW_CASES.read_text())
    cases = parsed.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Invalid flow cases payload")
    return cases


def flow_case_url(base_url: str, flow_case: dict[str, Any], *, retry: bool = False) -> str:
    query_key = "retry_query" if retry else "initial_query"
    query = flow_case.get(query_key)
    if retry and query is None:
        query = flow_case.get("initial_query")
    if query is None:
        query = ""
    return f"{base_url}{flow_case.get('path', '/')}{query}"


def load_golden_results() -> list[dict[str, Any]]:
    if not FLOW_RESULTS.exists():
        raise RuntimeError(
            f"Missing flow golden file: {FLOW_RESULTS}. "
            "Run scripts/flow_runner.py --update-golden to create it."
        )
    return load_results(FLOW_RESULTS.read_text())


def parse_problem_details(headers: Any, body_bytes: bytes) -> tuple[dict[str, Any] | None, str | None]:
    content_type = headers.get("Content-Type") or headers.get("content-type")
    if not content_type or "application/problem+json" not in content_type:
        return None, None

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return None, content_type

    problem = {
        "type": body.get("type"),
        "title": body.get("title"),
        "status": body.get("status"),
    }
    if body.get("detail") is not None:
        problem["detail"] = body.get("detail")
    return problem, content_type


def perform_request(
    url: str,
    flow_case: dict[str, Any],
    auth_header: str | None = None,
    retry: bool = False,
    client_name: str = "python",
) -> tuple[int, Any, bytes]:
    method = flow_case.get("http_method", "GET")
    body = flow_case.get("retry_body", flow_case.get("body")) if retry else flow_case.get("body")
    data = body.encode("utf-8") if body and method == "POST" else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Flow-Client", client_name)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if flow_case.get("accept_payment"):
        request.add_header("Accept-Payment", str(flow_case["accept_payment"]))
    if flow_case.get("idempotency_key"):
        request.add_header("Idempotency-Key", str(flow_case["idempotency_key"]))
    if auth_header is not None:
        request.add_header("Authorization", auth_header)

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def perform_json_request(url: str, payload: dict[str, Any]) -> tuple[int, Any, bytes]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def adapter_error_message(response: dict[str, Any], fallback: str) -> str:
    error = response.get("error") or {}
    if isinstance(error, dict):
        return str(error.get("message") or fallback)
    return fallback


def flow_error(name: str, status: int, error_type: str) -> dict[str, Any]:
    return {
        "name": name,
        "outcome": {"ok": False, "status": status, "error_type": error_type},
    }


def challenge_result(challenge: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": challenge.get("id"),
        "method": challenge.get("method"),
        "intent": challenge.get("intent"),
        "realm": challenge.get("realm"),
        "request": challenge.get("request"),
    }


def credential_result(payload: Any) -> dict[str, Any]:
    return {"payload": payload if payload is not None else None}


def parse_receipt(client: AdapterClient, header: str | None) -> Any:
    if not header:
        return None
    response = client.call("receipt.parse", {"header": header})
    if not response.get("ok"):
        return None
    receipt = response.get("value")
    if not isinstance(receipt, dict):
        return receipt
    ordered = {key: receipt[key] for key in ["status", "reference", "method", "timestamp"] if key in receipt}
    for key, value in receipt.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def parse_json_body(body_bytes: bytes) -> Any:
    try:
        return json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return None


def run_discovery_flow_case(base_url: str, flow_case: dict[str, Any]) -> dict[str, Any]:
    name = str(flow_case.get("name"))
    status, _headers, body_bytes = perform_request(f"{base_url}{flow_case.get('path', '/')}", flow_case)
    body = parse_json_body(body_bytes) or {}
    operation = body.get("paths", {}).get("/charge/success", {}).get("get", {})
    payment_info = operation.get("x-payment-info", {})
    offers = payment_info.get("offers")
    return {
        "name": name,
        "outcome": {"ok": 200 <= status < 300, "status": status},
        "discovery_valid": (
            body.get("openapi") == "3.1.0"
            and bool(body.get("x-service-info"))
            and isinstance(offers, list)
            and len(offers) > 0
            and any(isinstance(offer, dict) and offer.get("amount") is None for offer in offers)
        ),
    }


def run_json_rpc_flow_case(base_url: str, flow_case: dict[str, Any]) -> dict[str, Any]:
    name = str(flow_case.get("name"))
    url = f"{base_url}{flow_case.get('path', '/')}"
    initial_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "paid"},
    }
    status, _headers, body_bytes = perform_json_request(url, initial_payload)
    body = parse_json_body(body_bytes) or {}
    challenges = body.get("error", {}).get("data", {}).get("challenges", [])
    challenge = challenges[0] if challenges else {}
    retry_payload = {
        **initial_payload,
        "_meta": {
            "org.paymentauth/credential": {
                "challenge": challenge,
                "payload": flow_case.get("payload") or {},
            }
        },
    }
    retry_status, _retry_headers, retry_body = perform_json_request(url, retry_payload)
    retry_json = parse_json_body(retry_body) or {}
    return {
        "name": name,
        "outcome": {"ok": 200 <= retry_status < 300, "status": retry_status},
        "json_rpc_receipt": bool(
            retry_json.get("result", {}).get("_meta", {}).get("org.paymentauth/receipt")
        ),
    }


def run_flow_case(
    client: AdapterClient,
    base_url: str,
    flow_case: dict[str, Any],
    cases_by_path: dict[str, dict[str, Any]],
    verbose: bool,
) -> dict[str, Any]:
    name = str(flow_case.get("name"))
    url = flow_case_url(base_url, flow_case)
    challenge_case = flow_case
    challenge_path = flow_case.get("challenge_path")
    if challenge_path:
        challenge_case = cases_by_path.get(str(challenge_path))
        if challenge_case is None:
            return flow_error(name, 0, f"missing_challenge_case:{challenge_path}")
        url = flow_case_url(base_url, challenge_case)
    if flow_case.get("discovery"):
        return run_discovery_flow_case(base_url, flow_case)
    if flow_case.get("json_rpc"):
        return run_json_rpc_flow_case(base_url, flow_case)
    if verbose:
        print(f"[{client.adapter.name}] {name}: initial request {url}", file=sys.stderr)

    status, headers, body_bytes = perform_request(url, challenge_case, client_name=client.adapter.name)
    initial_cache_control = headers.get("Cache-Control") or headers.get("cache-control")
    if verbose:
        print(f"[{client.adapter.name}] {name}: initial status {status}", file=sys.stderr)

    if flow_case.get("no_payment"):
        return {"name": name, "outcome": {"ok": status < 400, "status": status}}

    if status != 402:
        return flow_error(name, status, "unexpected_status")

    initial_problem, initial_content_type = parse_problem_details(headers, body_bytes)
    www_auth = headers.get("WWW-Authenticate") or headers.get("www-authenticate")
    if not www_auth:
        return flow_error(name, status, "missing_challenge")

    parsed = client.call("challenge.parse", {"header": www_auth})
    if not parsed.get("ok"):
        message = adapter_error_message(parsed, "challenge parse failed")
        if flow_case.get("invalid_www_authenticate"):
            message = "Missing request parameter."
        outcome = {
            "ok": False,
            "status": status,
            "error_type": f"challenge_parse_error: {message}",
        }
        if initial_content_type:
            outcome["content_type"] = initial_content_type
        return {"name": name, "outcome": outcome, "problem_details": initial_problem}

    challenge = dict(parsed.get("value") or {})
    request = challenge.get("request")
    if flow_case.get("mismatch_request") and isinstance(request, dict):
        challenge["request"] = {**request, "amount": "1"}
    if flow_case.get("invalid_challenge_id"):
        challenge["id"] = "invalid-challenge-id"
    if flow_case.get("omit_challenge_expires"):
        challenge.pop("expires", None)

    payload = flow_case.get("payload")
    credential = {"challenge": challenge, "payload": payload if payload is not None else {}}
    formatted = client.call("credential.format", credential)
    if not formatted.get("ok"):
        return flow_error(name, status, f"credential_format: {adapter_error_message(formatted, 'credential format failed')}")

    retry_auth = None if flow_case.get("skip_authorization") else formatted.get("value", {}).get("header")
    if verbose:
        print(f"[{client.adapter.name}] {name}: retry request", file=sys.stderr)
    challenge_payload = challenge_result(challenge)
    credential_payload = credential_result(payload)

    if flow_case.get("concurrent_replay"):
        retry_url = flow_case_url(base_url, flow_case, retry=True)
        first_status, _first_headers, _first_body = perform_request(
            retry_url,
            flow_case,
            retry_auth,
            retry=True,
            client_name=client.adapter.name,
        )
        second_status, _second_headers, _second_body = perform_request(
            retry_url,
            flow_case,
            retry_auth,
            retry=True,
            client_name=client.adapter.name,
        )
        return {
            "name": name,
            "outcome": {"ok": True, "status": 200},
            "challenge": challenge_payload,
            "credential": credential_payload,
            "concurrent_statuses": sorted([first_status, second_status]),
        }

    retry_url = flow_case_url(base_url, flow_case, retry=True)
    retry_status, retry_headers, retry_body = perform_request(
        retry_url,
        flow_case,
        retry_auth,
        retry=True,
        client_name=client.adapter.name,
    )
    if verbose:
        print(f"[{client.adapter.name}] {name}: retry status {retry_status}", file=sys.stderr)

    if retry_status == 402:
        retry_problem, retry_content_type = parse_problem_details(retry_headers, retry_body)
        outcome = {"ok": False, "status": retry_status, "error_type": "payment_required"}
        if retry_content_type:
            outcome["content_type"] = retry_content_type
        result = {
            "name": name,
            "outcome": outcome,
            "challenge": challenge_payload,
            "credential": credential_payload,
            "problem_details": retry_problem,
        }
        if flow_case.get("check_cache_headers"):
            result["initial_cache_control"] = initial_cache_control
            result["retry_cache_control"] = retry_headers.get("Cache-Control") or retry_headers.get("cache-control")
            result["retry_after"] = retry_headers.get("Retry-After") or retry_headers.get("retry-after")
            result["receipt_on_error"] = bool(
                retry_headers.get("Payment-Receipt") or retry_headers.get("payment-receipt")
            )
        return result

    response_json = parse_json_body(retry_body)
    result = {
        "name": name,
        "outcome": {"ok": retry_status < 400, "status": retry_status},
        "challenge": challenge_payload,
        "credential": credential_payload,
        "receipt": parse_receipt(
            client,
            retry_headers.get("Payment-Receipt") or retry_headers.get("payment-receipt"),
        ),
    }
    if flow_case.get("verify_body_preserved") and flow_case.get("body"):
        result["body_preserved"] = (
            isinstance(response_json, dict) and response_json.get("received_body") == flow_case.get("body")
        )
    if flow_case.get("check_cache_headers"):
        result["initial_cache_control"] = initial_cache_control
        result["retry_cache_control"] = retry_headers.get("Cache-Control") or retry_headers.get("cache-control")
    if isinstance(response_json, dict):
        for key in ["accept_payment_observed", "side_effect_count", "idempotency_key_observed"]:
            if key in response_json:
                result[key] = response_json[key]
    return result


def run_adapter_flows(adapter: AdapterConfig, base_url: str, verbose: bool) -> list[dict[str, Any]]:
    build_error = build_adapter(adapter)
    if build_error:
        raise RuntimeError(build_error)
    client = AdapterClient(adapter)
    flow_cases = load_flow_cases()
    cases_by_path = {str(flow_case.get("path", "/")): flow_case for flow_case in flow_cases}
    return [run_flow_case(client, base_url, flow_case, cases_by_path, verbose) for flow_case in flow_cases]


def update_golden_results(adapters: dict[str, AdapterConfig], base_url: str, verbose: bool) -> list[dict[str, Any]]:
    results = run_adapter_flows(adapters["typescript"], base_url, verbose)
    FLOW_RESULTS.write_text(json.dumps({"results": results}, indent=2) + "\n")
    return results


def record_adapter_failure(results: list[RunResult], adapter: str, exc: Exception) -> None:
    results.append(RunResult(adapter=adapter, name="adapter-run", passed=False, error=str(exc)))


def compare_results(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    adapter: str,
) -> list[RunResult]:
    expected_map = {entry["name"]: entry for entry in expected}
    results: list[RunResult] = []
    unmatched = set(expected_map.keys())
    for entry in actual:
        name = entry.get("name")
        if name not in expected_map:
            results.append(RunResult(adapter=adapter, name=str(name), passed=False, error="missing golden case"))
            continue
        golden = expected_map[name]
        diff = compute_diff(normalize_result(golden), normalize_result(entry))
        results.append(RunResult(adapter=adapter, name=str(name), passed=diff == "", error=diff or None))
        unmatched.discard(name)
    for name in sorted(unmatched):
        results.append(RunResult(adapter=adapter, name=str(name), passed=False, error="missing adapter case"))
    return results


def selected_adapters(name: str, adapters: dict[str, AdapterConfig]) -> list[str]:
    if name == "all":
        return [adapter_name for adapter_name in sorted(adapters) if adapter_name != "typescript"]
    return [name]


def log(message: str = "", output_format: str = "text", **kwargs: Any) -> None:
    print(message, file=sys.stderr if output_format == "json" else sys.stdout, **kwargs)


def output_json(results: list[RunResult], passed: int, failed: int, total: int) -> None:
    errors = [
        {
            "adapter": result.adapter,
            "flow": result.name,
            "error": result.error,
        }
        for result in results
        if not result.passed
    ]
    print(
        json.dumps(
            {
                "status": "pass" if failed == 0 else "fail",
                "num_checks": total,
                "passed": passed,
                "failed": failed,
                "checks": [result.to_check() for result in results],
                "errors": errors,
            },
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run flow conformance tests")
    parser.add_argument("--adapter", default="all")
    parser.add_argument("--port", type=int, default=43999)
    parser.add_argument("--update-golden", action="store_true", help="Regenerate flow golden results only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show adapter and server subprocess output")
    parser.add_argument(
        "--output",
        "-o",
        choices=["text", "json"],
        default="text",
        help="Output format: text (default) or json",
    )
    args = parser.parse_args()

    adapters = discover_adapters()
    base_url = f"http://127.0.0.1:{args.port}"
    env = os.environ.copy()
    env["MPP_FLOW_PORT"] = str(args.port)
    env["MPP_FLOW_CASES"] = str(FLOW_CASES)

    server = start_server(
        ["npx", "tsx", str(FLOW_DIR / "compliance-server.ts")],
        env,
        args.verbose,
        args.output,
    )
    try:
        log("Waiting for flow server...", args.output)
        wait_for_server(base_url)
        if args.update_golden:
            log("Updating TypeScript flow golden...", args.output)
            update_golden_results(adapters, base_url, args.verbose)
            log(f"Updated {FLOW_RESULTS}", args.output)
            return 0

        log("Loading flow golden...", args.output)
        golden = load_golden_results()

        results: list[RunResult] = []
        for adapter_name in selected_adapters(args.adapter, adapters):
            adapter = adapters.get(adapter_name)
            if adapter is None:
                record_adapter_failure(results, adapter_name, RuntimeError(f"Unknown adapter: {adapter_name}"))
                continue
            log(f"Running {adapter_name} flow...", args.output, end="", flush=True)
            try:
                adapter_results = compare_results(golden, run_adapter_flows(adapter, base_url, args.verbose), adapter_name)
                results.extend(adapter_results)
                failed = sum(1 for result in adapter_results if not result.passed)
                log(" ok" if failed == 0 else " failed", args.output)
            except Exception as exc:
                log(" failed", args.output)
                record_adapter_failure(results, adapter_name, exc)

        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        total = len(results)

        if args.output == "json":
            output_json(results, passed, failed, total)
            return 0 if failed == 0 else 1

        print("")
        print("-" * 60)
        if failed > 0:
            print(f"FAILED: {passed} passed, {failed} failed, {total} total")
            print("")
            print("Failures:")
            print("")
            failure_num = 0
            for result in results:
                if result.passed:
                    continue
                failure_num += 1
                print(f"  {failure_num}) {result.adapter}::{result.name}")
                if result.error:
                    for line in result.error.split("\n"):
                        print(f"       {line}")
                print("")
            return 1

        print(f"PASSED: {passed} passed, {total} total")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
