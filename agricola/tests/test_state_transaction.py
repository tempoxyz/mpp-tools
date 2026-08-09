from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agricola.state_transaction import (
    GitStateStore,
    StateTransactionError,
    transact,
)


class FakeSnapshot:
    def __init__(self, published: bool, *, changed: bool = True) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Path(self.directory.name) / "ledger"
        self.ledger.mkdir()
        self.published = published
        self.changed = changed
        self.publish_calls = 0
        self.closed = False

    def commit(self) -> bool:
        return self.changed

    def publish(self) -> bool:
        self.publish_calls += 1
        return self.published

    def close(self) -> None:
        self.closed = True
        self.directory.cleanup()


class FakeStore:
    def __init__(self, *snapshots: FakeSnapshot) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self) -> FakeSnapshot:
        snapshot = self.snapshots[self.calls]
        self.calls += 1
        return snapshot


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class StateTransactionTests(unittest.TestCase):
    def test_replays_operation_after_guarded_push_conflict(self) -> None:
        first = FakeSnapshot(False)
        second = FakeSnapshot(True)
        store = FakeStore(first, second)
        ledgers: list[str] = []

        def run(command, cwd, environment):
            ledgers.append(environment["AGRICOLA_LEDGER"])
            return subprocess.CompletedProcess(
                command, 0, f"attempt {len(ledgers)}\n", ""
            )

        result = transact(
            ("operation",),
            store,
            runner=run,
            pause=lambda _: None,
        )

        self.assertEqual(result.stdout, "attempt 2\n")
        self.assertEqual(store.calls, 2)
        self.assertEqual(len(set(ledgers)), 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_no_change_completes_without_push(self) -> None:
        snapshot = FakeSnapshot(True, changed=False)
        result = transact(
            ("operation",),
            FakeStore(snapshot),
            runner=lambda command, cwd, environment: subprocess.CompletedProcess(
                command, 0, "unchanged\n", ""
            ),
        )

        self.assertEqual(result.stdout, "unchanged\n")
        self.assertEqual(snapshot.publish_calls, 0)

    def test_operation_failure_is_not_retried(self) -> None:
        snapshot = FakeSnapshot(True)

        with self.assertRaisesRegex(StateTransactionError, "operation failed"):
            transact(
                ("operation",),
                FakeStore(snapshot),
                runner=lambda command, cwd, environment: subprocess.CompletedProcess(
                    command, 7, "", "bad input"
                ),
            )

        self.assertTrue(snapshot.closed)

    def test_requires_a_command_and_positive_attempt_count(self) -> None:
        with self.assertRaisesRegex(StateTransactionError, "command is required"):
            transact((), FakeStore())
        with self.assertRaisesRegex(StateTransactionError, "must be positive"):
            transact(("operation",), FakeStore(), attempts=0)

    def test_exhausted_conflicts_fail_visibly(self) -> None:
        snapshots = (FakeSnapshot(False), FakeSnapshot(False))
        with self.assertRaisesRegex(StateTransactionError, "all 2"):
            transact(
                ("operation",),
                FakeStore(*snapshots),
                attempts=2,
                runner=lambda command, cwd, environment: subprocess.CompletedProcess(
                    command, 0, "", ""
                ),
                pause=lambda _: None,
            )
        self.assertTrue(all(snapshot.closed for snapshot in snapshots))

    def test_invalid_restored_state_stops_before_operation(self) -> None:
        snapshot = FakeSnapshot(True)
        (snapshot.ledger / "invalid.json").write_text("{}")
        called = False

        def run(command, cwd, environment):
            nonlocal called
            called = True
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(StateTransactionError, "invalid restored state"):
            transact(("operation",), FakeStore(snapshot), runner=run)
        self.assertFalse(called)

    def test_missing_operation_executable_is_contextual(self) -> None:
        with self.assertRaisesRegex(StateTransactionError, "cannot start"):
            transact(
                ("agricola-command-that-does-not-exist",),
                FakeStore(FakeSnapshot(True)),
            )

    def test_git_store_publishes_full_ledger_snapshots_from_current_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            repository = root / "repository"
            git(root, "init", "--bare", "--initial-branch=main", str(remote))
            git(root, "init", "--initial-branch=main", str(repository))
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            (repository / "ledger").mkdir()
            (repository / "ledger" / "base.txt").write_text("base\n")
            (repository / "trusted.txt").write_text("trusted\n")
            git(repository, "add", "ledger", "trusted.txt")
            git(repository, "commit", "-m", "initial")
            git(repository, "remote", "add", "origin", str(remote))
            git(repository, "push", "-u", "origin", "main")

            def operation(name: str, value: str) -> tuple[str, ...]:
                script = (
                    "import os; from pathlib import Path; "
                    f"Path(os.environ['AGRICOLA_LEDGER'], '{name}').write_text('{value}')"
                )
                return (sys.executable, "-c", script)

            store = GitStateStore(repository, branch="agricola/state", base="main")
            (repository / "trusted.txt").write_text("newer trusted base\n")
            git(repository, "commit", "-am", "advance main")
            git(repository, "push", "origin", "main")
            main_sha = git(repository, "rev-parse", "HEAD")
            transact(operation("first.txt", "first"), store, cwd=repository)
            transact(operation("second.txt", "second"), store, cwd=repository)

            stale = store.snapshot()
            try:
                transact(operation("winner.txt", "winner"), store, cwd=repository)
                (stale.ledger / "stale.txt").write_text("stale")
                self.assertTrue(stale.commit())
                self.assertFalse(stale.publish())
            finally:
                stale.close()

            state = git(remote, "rev-parse", "refs/heads/agricola/state")
            self.assertEqual(git(remote, "show", f"{state}:ledger/first.txt"), "first")
            self.assertEqual(
                git(remote, "show", f"{state}:ledger/second.txt"), "second"
            )
            self.assertEqual(
                git(remote, "show", f"{state}:ledger/winner.txt"), "winner"
            )
            missing = subprocess.run(
                ["git", "show", f"{state}:ledger/stale.txt"],
                cwd=remote,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertEqual(git(remote, "rev-parse", f"{state}^"), main_sha)


if __name__ == "__main__":
    unittest.main()
