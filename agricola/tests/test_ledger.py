from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from agricola.ledger import CursorStore, DecisionLedger, LedgerError
from agricola.models import Cursor, DecisionKind, SkipDecision
from agricola.tests.helpers import change


class LedgerTests(unittest.TestCase):
    def test_ensure_creates_snapshot_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = DecisionLedger(directory)
            self.assertTrue(ledger.ensure(change()))
            self.assertFalse(ledger.ensure(change()))
            entry = ledger.read("wevm/mppx", 412)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.labels, ("agricola:go",))
            self.assertEqual(entry.decisions, ())

    def test_append_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = DecisionLedger(directory)
            decision = SkipDecision(
                target="ruby",
                decision=DecisionKind.SKIP,
                by="brendanryan",
                reason="TS only",
                idempotency_key="comment:1",
                at=datetime(2026, 8, 7, 14, 2, tzinfo=UTC),
            )
            self.assertTrue(ledger.append(change(), decision))
            self.assertFalse(ledger.append(change(), decision))
            entry = ledger.read("wevm/mppx", 412)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(len(entry.decisions), 1)
            self.assertEqual(
                entry.decisions[0].at,
                datetime(2026, 8, 7, 14, 2, tzinfo=UTC),
            )

    def test_reused_idempotency_key_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = DecisionLedger(directory)
            first = SkipDecision(
                target="ruby",
                decision=DecisionKind.SKIP,
                by="brendanryan",
                idempotency_key="same",
                reason="one",
            )
            second = SkipDecision(
                target="ruby",
                decision=DecisionKind.SKIP,
                by="brendanryan",
                idempotency_key="same",
                reason="two",
            )
            ledger.append(change(), first)
            with self.assertRaisesRegex(LedgerError, "idempotency key reused"):
                ledger.append(change(), second)

    def test_written_json_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = DecisionLedger(directory)
            ledger.ensure(change())
            path = ledger.path_for("wevm/mppx", 412)
            self.assertEqual(
                json.loads(path.read_text())["source"]["sha"], "abc1234567"
            )

    def test_validation_rejects_malformed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = DecisionLedger(directory).path_for("wevm/mppx", 412)
            path.write_text('{"id": "mppx#412"}\n')
            with self.assertRaisesRegex(LedgerError, "cannot read ledger entry"):
                DecisionLedger(directory).validate_all()


class CursorStoreTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CursorStore(directory)
            cursor = Cursor(merged_at=datetime(2026, 8, 7, 14, 0, tzinfo=UTC))
            store.save(cursor)
            self.assertEqual(store.load(), cursor)

    def test_missing_cursor_bootstraps_with_lookback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            before = datetime.now(UTC) - timedelta(minutes=16)
            store = CursorStore(directory, bootstrap_lookback=timedelta(minutes=15))
            cursor = store.load()
            self.assertGreater(cursor.merged_at, before)
            self.assertTrue(store.path.exists())

    def test_validation_rejects_malformed_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CursorStore(directory)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text('{"merged_at": "yesterday"}\n')
            with self.assertRaisesRegex(LedgerError, "cannot read cursor"):
                DecisionLedger(directory).validate_all()


if __name__ == "__main__":
    unittest.main()
