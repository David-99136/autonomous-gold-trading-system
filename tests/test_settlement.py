import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from gold_system.settlement import SettlementLedger
from gold_system.store import Store


class SettlementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "events.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.ledger = SettlementLedger(self.store)
        self.values = dict(mode="DEMO", account_hash="a" * 64, fill_hash="b" * 64,
                           trade_hash="c" * 64, evidence_hash="d" * 64,
                           occurred_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
                           quantity=D("0.001"), realized_pnl=D("1.20"))

    def record(self, **changes):
        return self.ledger.record_close(**(self.values | changes))

    def test_repeat_is_idempotent_and_decimal_representation_stable(self):
        key = self.record()
        self.assertEqual(key, self.record(realized_pnl=D("1.2")))
        self.assertEqual(self.ledger.pending_ids(), [key])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM close_segments").fetchone()[0], 1)

    def test_conflicting_fill_rejected_and_original_preserved(self):
        key = self.record()
        with self.assertRaisesRegex(ValueError, "SETTLEMENT_CONFLICT"):
            self.record(realized_pnl=D("9"))
        self.assertEqual(self.ledger.claim_notification(key)["realized_pnl"], "1.2")

    def test_mode_and_account_separate_identical_fill_keys(self):
        keys = {self.record(), self.record(mode="PAPER"), self.record(account_hash="e" * 64)}
        self.assertEqual(len(keys), 3)

    def test_partial_fills_remain_separate_with_same_trade(self):
        self.record()
        self.record(fill_hash="e" * 64)
        self.assertEqual(len(self.ledger.pending_ids()), 2)

    def test_notification_failure_rolls_back_accounting(self):
        self.store.db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON settlement_notifications BEGIN SELECT RAISE(ABORT, 'test'); END")
        self.store.db.commit()
        with self.assertRaises(Exception):
            self.record()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM close_segments").fetchone()[0], 0)

    def test_single_claim_survives_second_connection_and_restart(self):
        key = self.record()
        self.assertIsNotNone(self.ledger.claim_notification(key))
        other = Store(self.path)
        try:
            resumed = SettlementLedger(other)
            self.assertIsNone(resumed.claim_notification(key))
            self.assertEqual(resumed.pending_ids(), [])
        finally:
            other.close()

    def test_unknown_and_delivered_are_not_blindly_retried(self):
        for i, outcome in enumerate(("UNKNOWN", "DELIVERED")):
            key = self.record(fill_hash=str(i) * 64)
            self.ledger.claim_notification(key)
            self.ledger.finish_notification(key, outcome)
            self.assertIsNone(self.ledger.claim_notification(key))

    def test_definite_non_delivery_can_retry(self):
        key = self.record()
        self.ledger.claim_notification(key)
        self.ledger.finish_notification(key, "NOT_DELIVERED")
        self.assertIsNotNone(self.ledger.claim_notification(key))

    def test_missing_fee_is_unknown_not_zero(self):
        key = self.record()
        self.assertIsNone(self.ledger.claim_notification(key)["extra_fee"])

    def test_invalid_input_and_transitions(self):
        for changes in ({"mode": "LIVE"}, {"account_hash": "raw-account"},
                        {"quantity": D(0)}, {"realized_pnl": D("NaN")},
                        {"extra_fee": D(-1)}, {"quantity": 0.1},
                        {"occurred_at": datetime(2026, 9, 20)}):
            with self.assertRaises(ValueError):
                self.record(**changes)
        key = self.record()
        with self.assertRaises(ValueError):
            self.ledger.finish_notification(key, "DELIVERED")
