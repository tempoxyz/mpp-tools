#!/usr/bin/env python3
"""Tests for the client-server pair runner."""

from __future__ import annotations

import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pair_runner
from harness import AdapterClient, AdapterConfig, stop_adapter_process


def b64(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def adapter(name: str, capabilities: list[str], command: list[str] | None = None) -> AdapterConfig:
    return AdapterConfig(name=name, command=command or ["true"], capabilities=capabilities)


class LoadScenariosTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(pair_runner.SCENARIOS_PATH.read_text(encoding="utf-8"))

    def load(self, document: object) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenarios.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return pair_runner.load_scenarios(path)

    def test_checked_in_scenarios_have_snapshots(self) -> None:
        scenarios = pair_runner.load_scenarios()
        snapshots = pair_runner.load_snapshots()
        self.assertEqual({scenario["name"] for scenario in scenarios}, set(snapshots))
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                self.assertEqual(
                    [step["name"] for step in scenario["steps"]],
                    [step["name"] for step in snapshots[scenario["name"]]],
                )

    def test_duplicate_scenario_names_are_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["scenarios"].append(copy.deepcopy(document["scenarios"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate pair scenario name"):
            self.load(document)

    def test_reference_to_a_later_step_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        steps = document["scenarios"][0]["steps"]
        steps.insert(0, {"name": "early", "kind": "request", "path": "/paid", "credential": {"from": "unpaid"}})
        with self.assertRaisesRegex(ValueError, "not an earlier step"):
            self.load(document)

    def test_reference_to_a_pay_step_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["scenarios"][0]["steps"].append({"name": "again", "kind": "request", "path": "/paid", "replay": "pay"})
        with self.assertRaisesRegex(ValueError, "not a request step"):
            self.load(document)

    def test_expected_failure_of_an_unknown_step_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["scenarios"][0]["expectedFailures"] = [
            {"client": "*", "server": "python", "steps": ["missing"], "reason": "known"}
        ]
        with self.assertRaisesRegex(ValueError, "unknown steps: missing"):
            self.load(document)

    def test_pay_step_cannot_carry_a_credential(self) -> None:
        document = copy.deepcopy(self.document)
        document["scenarios"][0]["steps"].append(
            {"name": "bad", "kind": "pay", "path": "/paid", "credential": {"from": "unpaid"}}
        )
        with self.assertRaisesRegex(ValueError, "failed schema validation"):
            self.load(document)


class WireNormalizationTest(unittest.TestCase):
    def test_splits_joined_challenges_and_skips_other_schemes(self) -> None:
        header = (
            'Payment id="a", realm="r", method="conformance", intent="charge", description="x, y=\\"z\\"", '
            'Bearer realm="api", Payment id="b", realm="r", method="tempo", intent="charge"'
        )
        challenges = pair_runner.split_challenges(header)
        self.assertEqual([challenge["id"] for challenge in challenges], ["a", "b"])
        self.assertEqual(challenges[0]["description"], 'x, y="z"')
        self.assertNotIn("realm", {key for key in challenges[1] if challenges[1][key] == "api"})

    def test_masks_ids_and_drops_reserved_extensions(self) -> None:
        challenge = pair_runner.normalize_challenge({
            "id": "abc",
            "request": b64({"amount": "1", "_mppx_scope": {"resource": "/paid"}}),
            "opaque": b64({"_mpp_nonce": "f00"}),
        })
        self.assertEqual(challenge, {"id": pair_runner.MASK, "request": {"amount": "1"}})

    def test_keeps_route_metadata(self) -> None:
        challenge = pair_runner.normalize_challenge({"opaque": b64({"account": "alice", "_mppx_nonce": "1"})})
        self.assertEqual(challenge["opaque"], {"account": "alice"})

    def test_problem_body_keeps_type_and_status(self) -> None:
        headers = [
            ("Content-Type", "application/problem+json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("WWW-Authenticate", 'Payment id="a", realm="r", method="tempo", intent="charge", request="e30"'),
        ]
        body = json.dumps({"type": "t", "title": "T", "status": 402, "detail": "d", "challengeId": "a"})
        snapshot = pair_runner.normalize_response(402, headers, body.encode())
        self.assertEqual(snapshot["body"], {"type": "t", "status": 402})
        self.assertEqual(snapshot["contentType"], "application/problem+json")
        self.assertEqual(snapshot["challenges"][0]["request"], {})

    def test_receipt_is_decoded_with_challenge_id_masked(self) -> None:
        receipt = b64({"status": "success", "challengeId": "abc", "reference": "r"})
        snapshot = pair_runner.normalize_response(200, {"payment-receipt": receipt}, "")
        self.assertEqual(snapshot["receipt"], {"status": "success", "challengeId": pair_runner.MASK, "reference": "r"})


class CompareTest(unittest.TestCase):
    scenario = {
        "name": "s",
        "expectedFailures": [
            {"client": "*", "server": "python", "steps": ["b"], "reason": "known"},
            {"client": "go", "server": "python", "steps": ["c"], "reason": "go"},
        ],
    }
    expected = [{"name": "a", "status": 200}, {"name": "b", "status": 402}, {"name": "c", "status": 402}]

    def run_with(self, **statuses: int) -> list[dict[str, object]]:
        return [{**step, "status": statuses.get(str(step["name"]), step["status"])} for step in self.expected]

    def test_statuses(self) -> None:
        same, other = self.run_with(), self.run_with(b=403, c=403)
        self.assertEqual(pair_runner.compare(self.scenario, self.expected, same, "go", "typescript").status, "pass")
        self.assertEqual(pair_runner.compare(self.scenario, self.expected, other, "go", "typescript").status, "fail")
        xfail = pair_runner.compare(self.scenario, self.expected, other, "go", "python")
        self.assertEqual((xfail.status, xfail.detail), ("xfail", "known go"))
        xpass = pair_runner.compare(self.scenario, self.expected, same, "ruby", "python")
        self.assertEqual(xpass.status, "xpass")
        self.assertTrue(xpass.failed)

    def test_expected_failure_covers_only_its_steps(self) -> None:
        result = pair_runner.compare(self.scenario, self.expected, self.run_with(a=500, b=403), "ruby", "python")
        self.assertEqual(result.status, "fail")
        self.assertIn("step a:", str(result.detail))
        self.assertNotIn("step b:", str(result.detail))

    def test_entry_whose_steps_all_match_is_stale(self) -> None:
        # b still differs, but the go entry for c no longer does.
        result = pair_runner.compare(self.scenario, self.expected, self.run_with(b=403), "go", "python")
        self.assertEqual((result.status, result.detail), ("xpass", "expected failure no longer fails: go"))

    def test_step_errors_follow_expected_failures(self) -> None:
        error = pair_runner.StepFailed("b", self.expected[:1], RuntimeError("adapter crashed"))
        # The go entry's step c never ran, so it is not reported stale.
        self.assertEqual(pair_runner.compare(self.scenario, self.expected, error, "go", "python").status, "xfail")
        failed = pair_runner.compare(self.scenario, self.expected, error, "go", "typescript")
        self.assertEqual((failed.status, failed.detail), ("fail", "step b: RuntimeError: adapter crashed"))

    def test_errors_outside_a_step_fail(self) -> None:
        failed = pair_runner.compare(self.scenario, self.expected, RuntimeError("server crashed"), "go", "python")
        self.assertEqual((failed.status, failed.detail), ("fail", "RuntimeError: server crashed"))

    def test_missing_snapshot_fails(self) -> None:
        result = pair_runner.compare(self.scenario, None, [], "go", "python")
        self.assertEqual(result.status, "fail")


class SelectionTest(unittest.TestCase):
    adapters = {
        "typescript": adapter("typescript", [*pair_runner.CLIENT_CAPABILITIES, "http.server"]),
        "python": adapter("python", [*pair_runner.CLIENT_CAPABILITIES, "http.server"]),
        "go": adapter("go", list(pair_runner.CLIENT_CAPABILITIES)),
        "ruby": adapter("ruby", ["challenge.parse"]),
    }

    def test_all_pairs(self) -> None:
        self.assertEqual(
            pair_runner.select_pairs(self.adapters, "all", "all", "all"),
            [("go", "python"), ("go", "typescript"), ("python", "python"), ("python", "typescript"),
             ("typescript", "python"), ("typescript", "typescript")],
        )

    def test_adapter_pairs_with_itself_and_the_reference(self) -> None:
        self.assertEqual(
            pair_runner.select_pairs(self.adapters, "python", "all", "all"),
            [("python", "python"), ("python", "typescript"), ("typescript", "python")],
        )
        self.assertEqual(pair_runner.select_pairs(self.adapters, "go", "all", "all"), [("go", "typescript")])

    def test_matrix(self) -> None:
        results = [
            pair_runner.PairResult("go", "typescript", "a", "pass"),
            pair_runner.PairResult("go", "typescript", "b", "xfail", "known"),
            pair_runner.PairResult("go", "python", "a", "skip", "old sdk"),
        ]
        matrix = pair_runner.format_matrix(results)
        self.assertIn("2/2 (1 xfail)", matrix)
        self.assertIn("skip", matrix)

    def test_port_pool(self) -> None:
        self.assertEqual(pair_runner.PortPool(None).take(), 0)
        pool = pair_runner.PortPool(44100)
        self.assertEqual([pool.take() for _ in range(101)][-2:], [44199, 44100])


class StartAdapterProcessTest(unittest.TestCase):
    def client(self, script: str) -> AdapterClient:
        return AdapterClient(adapter("fake", ["http.server"], [sys.executable, "-c", script]))

    config = {"port": 0, "realm": "r", "secretKey": "s" * 32, "routes": [
        {"path": "/paid", "offers": [{"method": "tempo", "intent": "charge",
                                      "request": {"amount": "1", "currency": "USD", "recipient": "m"}}]},
    ]}

    def test_returns_running_process_until_stopped(self) -> None:
        script = (
            "import json, sys, time; json.load(sys.stdin); "
            "print(json.dumps({'ok': True, 'value': {'url': 'http://127.0.0.1:1'}}), flush=True); time.sleep(60)"
        )
        response, process = self.client(script).start("http.server", self.config)
        self.assertEqual(response["value"]["url"], "http://127.0.0.1:1")
        self.assertIsNotNone(process)
        assert process is not None
        self.assertIsNone(process.poll())
        stop_adapter_process(process)
        self.assertIsNotNone(process.poll())

    def test_silent_exit_is_an_error(self) -> None:
        response, process = self.client("import sys; sys.stderr.write('boom')").start("http.server", self.config)
        self.assertIsNone(process)
        self.assertEqual(response["error"], {"type": "unknown_error", "message": "boom"})

    def test_unsupported_operation_is_passed_through(self) -> None:
        script = (
            "import json, sys; json.load(sys.stdin); "
            "print(json.dumps({'ok': False, 'error': {'type': 'unsupported_operation', 'message': 'old'}}))"
        )
        response, process = self.client(script).start("http.server", self.config)
        self.assertIsNone(process)
        self.assertEqual(response["error"]["type"], "unsupported_operation")


if __name__ == "__main__":
    unittest.main()

