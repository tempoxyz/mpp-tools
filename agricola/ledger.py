from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .models import CanonicalChange, Cursor, Decision, LedgerEntry, Source

CURSOR_FILENAME = "cursor.json"


class LedgerError(ValueError):
    pass


class DecisionLedger:
    def __init__(self, root: str | Path = "ledger") -> None:
        self.root = Path(root)

    def path_for(self, source_repo: str, pr: int) -> Path:
        owner, repo = source_repo.split("/", 1)
        return self.root / f"{owner}-{repo}-{pr}.json"

    def read(self, source_repo: str, pr: int) -> LedgerEntry | None:
        path = self.path_for(source_repo, pr)
        if not path.exists():
            return None
        try:
            return LedgerEntry.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            raise LedgerError(f"cannot read ledger entry {path}: {exc}") from exc

    def validate_all(self) -> tuple[Path, ...]:
        paths = tuple(
            path
            for path in sorted(self.root.glob("*.json"))
            if path.name != CURSOR_FILENAME
        )
        for path in paths:
            try:
                LedgerEntry.model_validate_json(path.read_text())
            except (OSError, ValidationError) as exc:
                raise LedgerError(f"cannot read ledger entry {path}: {exc}") from exc
        CursorStore(self.root).validate()
        return paths

    def append(self, change: CanonicalChange, decision: Decision) -> bool:
        path = self.path_for(change.repo, change.number)
        entry = self.read(change.repo, change.number) or self._new_entry(change)
        self._require_source(entry, change, path)
        return self._append(path, entry, decision)

    def append_source(self, source: Source, decision: Decision) -> bool:
        path = self.path_for(source.repo, source.pr)
        entry = self.read(source.repo, source.pr)
        if entry is None:
            raise LedgerError(f"cannot append decision before source snapshot: {path}")
        if entry.source != source:
            raise LedgerError(f"source metadata conflict in {path}")
        return self._append(path, entry, decision)

    def _append(self, path: Path, entry: LedgerEntry, decision: Decision) -> bool:
        for existing in entry.decisions:
            if existing.idempotency_key != decision.idempotency_key:
                continue
            if existing == decision:
                return False
            raise LedgerError(
                f"idempotency key reused with different data: {decision.idempotency_key}"
            )
        updated = entry.model_copy(
            update={"decisions": (*entry.decisions, decision)}, deep=True
        )
        self._write(path, updated.model_dump_json(indent=2, exclude_none=True) + "\n")
        return True

    def ensure(
        self, change: CanonicalChange, *, labels: Iterable[str] | None = None
    ) -> bool:
        path = self.path_for(change.repo, change.number)
        entry = self.read(change.repo, change.number)
        if entry is not None:
            self._require_source(entry, change, path)
            if labels is None:
                return False
            resolved_labels = tuple(sorted(set(labels)))
            if entry.labels == resolved_labels:
                return False
            if not entry.labels and not entry.decisions:
                repaired = entry.model_copy(
                    update={"labels": resolved_labels}, deep=True
                )
                self._write(
                    path,
                    repaired.model_dump_json(indent=2, exclude_none=True) + "\n",
                )
                return True
            raise LedgerError(f"merge-time label conflict in {path}")
        new_entry = self._new_entry(change, labels)
        self._write(path, new_entry.model_dump_json(indent=2, exclude_none=True) + "\n")
        return True

    @staticmethod
    def _new_entry(
        change: CanonicalChange, labels: Iterable[str] | None = None
    ) -> LedgerEntry:
        return LedgerEntry(
            id=change.source_id,
            source=Source(repo=change.repo, pr=change.number, sha=change.sha),
            labels=tuple(sorted(set(change.labels if labels is None else labels))),
        )

    @staticmethod
    def _require_source(
        entry: LedgerEntry, change: CanonicalChange, path: Path
    ) -> None:
        expected = Source(repo=change.repo, pr=change.number, sha=change.sha)
        if entry.id != change.source_id or entry.source != expected:
            raise LedgerError(f"source metadata conflict in {path}")

    def _write(self, path: Path, content: str) -> None:
        _atomic_write(path, content)


class CursorStore:
    def __init__(
        self,
        root: str | Path = "ledger",
        *,
        bootstrap_lookback: timedelta = timedelta(minutes=15),
    ) -> None:
        self.path = Path(root) / CURSOR_FILENAME
        self.bootstrap_lookback = bootstrap_lookback

    def load(self) -> Cursor:
        if not self.path.exists():
            cursor = Cursor(merged_at=datetime.now(UTC) - self.bootstrap_lookback)
            self.save(cursor)
            return cursor
        return self._read()

    def validate(self) -> None:
        if self.path.exists():
            self._read()

    def _read(self) -> Cursor:
        try:
            return Cursor.model_validate_json(self.path.read_text())
        except (OSError, ValidationError) as exc:
            raise LedgerError(f"cannot read cursor {self.path}: {exc}") from exc

    def save(self, cursor: Cursor) -> None:
        content = cursor.model_dump_json(indent=2) + "\n"
        _atomic_write(self.path, content)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
