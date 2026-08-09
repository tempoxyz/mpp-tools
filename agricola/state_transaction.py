from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .ledger import DecisionLedger, LedgerError


class StateTransactionError(RuntimeError):
    pass


class StateSnapshot(Protocol):
    @property
    def ledger(self) -> Path: ...

    def commit(self) -> bool: ...
    def publish(self) -> bool: ...
    def close(self) -> None: ...


class StateStore(Protocol):
    def snapshot(self) -> StateSnapshot: ...


@dataclass
class GitStateSnapshot:
    store: GitStateStore
    parent: Path
    worktree: Path
    expected: str | None

    @property
    def ledger(self) -> Path:
        return self.worktree / "ledger"

    def commit(self) -> bool:
        self.store.git(["add", "--", "ledger"], cwd=self.worktree)
        diff = self.store.git(
            ["diff", "--cached", "--quiet", "--", "ledger"],
            cwd=self.worktree,
            allowed_exit_codes=(0, 1),
        )
        if diff.returncode == 0:
            return False
        self.store.git(
            [
                "-c",
                "user.name=github-actions[bot]",
                "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit",
                "-m",
                self.store.message,
                "--",
                "ledger",
            ],
            cwd=self.worktree,
        )
        return True

    def publish(self) -> bool:
        branch_ref = f"refs/heads/{self.store.branch}"
        expected = self.expected or ""
        push = self.store.git(
            [
                "push",
                f"--force-with-lease={branch_ref}:{expected}",
                "origin",
                f"HEAD:{branch_ref}",
            ],
            cwd=self.worktree,
            allowed_exit_codes=(0, 1),
        )
        if push.returncode == 0:
            return True
        if self.store.remote_sha() != self.expected:
            return False
        detail = push.stderr.strip() or push.stdout.strip() or "git push failed"
        raise StateTransactionError(detail)

    def close(self) -> None:
        try:
            self.store.git(
                ["worktree", "remove", "--force", str(self.worktree)],
                cwd=self.store.root,
            )
        finally:
            shutil.rmtree(self.parent, ignore_errors=True)


class GitStateStore:
    def __init__(
        self,
        root: str | Path,
        *,
        branch: str,
        base: str = "HEAD",
        message: str = "chore(agricola): update state",
        temporary_directory: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.branch = branch
        self.message = message
        self.temporary_directory = (
            Path(temporary_directory).resolve() if temporary_directory else None
        )
        self.base = base

    def git(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path,
        allowed_exit_codes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode not in allowed_exit_codes:
            detail = process.stderr.strip() or process.stdout.strip()
            raise StateTransactionError(
                f"git {' '.join(arguments)} failed ({process.returncode}): {detail}"
            )
        return process

    def remote_sha(self) -> str | None:
        branch_ref = f"refs/heads/{self.branch}"
        result = self.git(
            ["ls-remote", "--heads", "origin", branch_ref], cwd=self.root
        ).stdout.strip()
        return result.split(maxsplit=1)[0] if result else None

    def snapshot(self) -> GitStateSnapshot:
        branch_ref = f"refs/heads/{self.branch}"
        remote_ref = f"refs/remotes/origin/{self.branch}"
        expected = self.remote_sha()
        if expected is not None:
            self.git(
                ["fetch", "--force", "origin", f"{branch_ref}:{remote_ref}"],
                cwd=self.root,
            )
            expected = self.git(["rev-parse", remote_ref], cwd=self.root).stdout.strip()

        if self.base == "HEAD":
            base = self.git(["rev-parse", "HEAD"], cwd=self.root).stdout.strip()
        else:
            base_ref = f"refs/remotes/origin/{self.base}"
            self.git(
                [
                    "fetch",
                    "--force",
                    "origin",
                    f"refs/heads/{self.base}:{base_ref}",
                ],
                cwd=self.root,
            )
            base = self.git(["rev-parse", base_ref], cwd=self.root).stdout.strip()

        parent = Path(
            tempfile.mkdtemp(
                prefix="agricola-state-",
                dir=self.temporary_directory,
            )
        )
        worktree = parent / "checkout"
        try:
            self.git(
                ["worktree", "add", "--detach", str(worktree), base],
                cwd=self.root,
            )
            if expected is not None:
                self.git(
                    ["restore", f"--source={expected}", "--", "ledger"],
                    cwd=worktree,
                )
            return GitStateSnapshot(self, parent, worktree, expected)
        except Exception:
            shutil.rmtree(parent, ignore_errors=True)
            raise


CommandRunner = Callable[
    [Sequence[str], Path, dict[str, str]], subprocess.CompletedProcess[str]
]


def run_command(
    command: Sequence[str], cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise StateTransactionError(
            f"cannot start state operation {command[0]!r}: {exc}"
        ) from exc


def transact(
    command: Sequence[str],
    store: StateStore,
    *,
    cwd: str | Path = ".",
    attempts: int = 20,
    runner: CommandRunner = run_command,
    pause: Callable[[float], None] = time.sleep,
) -> subprocess.CompletedProcess[str]:
    if not command:
        raise StateTransactionError("state transaction command is required")
    if attempts < 1:
        raise StateTransactionError("state transaction attempts must be positive")

    operation_cwd = Path(cwd).resolve()
    for attempt in range(attempts):
        snapshot = store.snapshot()
        try:
            environment = os.environ.copy()
            environment["AGRICOLA_LEDGER"] = str(snapshot.ledger)
            try:
                DecisionLedger(snapshot.ledger).validate_all()
            except LedgerError as exc:
                raise StateTransactionError(f"invalid restored state: {exc}") from exc
            result = runner(command, operation_cwd, environment)
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                raise StateTransactionError(
                    f"state operation failed ({result.returncode}): {detail}"
                )
            try:
                DecisionLedger(snapshot.ledger).validate_all()
            except LedgerError as exc:
                raise StateTransactionError(f"invalid updated state: {exc}") from exc
            if not snapshot.commit() or snapshot.publish():
                return result
        finally:
            snapshot.close()
        if attempt + 1 < attempts:
            pause(min(0.25 * (2**attempt), 3.0))
    raise StateTransactionError(
        f"state changed during all {attempts} transaction attempts"
    )
