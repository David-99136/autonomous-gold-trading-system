import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from gold_system.broker import PaperBroker
from gold_system.core import D, Contract, Direction, Mode, Quote, Quality, RiskPolicy, Signal
from gold_system.engine import Engine
from gold_system.store import Store


class ReversalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"audit.db")
        self.now = datetime(2026, 9, 7, 14, tzinfo=timezone.utc)
        self.policy = RiskPolicy()
        self.contract = Contract("TEST", D(1), D("0.01"), D("0.01"), D(100), D("0.01"), D("0.1"))
        self.broker = PaperBroker()
        self.engine = Engine(self.store, self.broker, self.contract, self.policy)
        self.quote = Quote(self.now, D(2000), D("2000.5"))
        self.quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        self.long = Signal("long", self.policy.version, self.now, self.now+timedelta(minutes=1),
                           Direction.LONG, Mode.RIGHT, D(1990), D(2030), "fixture")
        self.short = replace(self.long, signal_id="short", direction=Direction.SHORT, stop=D(2010), target=D(1970))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tick(self, signal=None, quality=None, quote=None):
        asyncio.run(self.engine.tick(self.now, quote or self.quote, quality or self.quality, signal))

    def exit_reasons(self):
        return [e["payload"]["reason"] for e in self.store.events() if e["kind"] == "EXIT"]

    def test_both_directions_close_before_new_entry(self):
        self.tick(self.long)
        self.tick(self.short)
        self.assertIsNone(self.broker.position)
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.exit_reasons(), ["REVERSE_SIGNAL"])
        # 下一 cycle 重新通過風控才可進場，沒有 long/short 同時存在。
        self.tick(self.short)
        self.assertEqual(self.broker.position.direction, Direction.SHORT)
        self.assertEqual(self.broker.submissions, 2)
        self.tick(replace(self.long, signal_id="long2"))
        self.assertIsNone(self.broker.position)
        self.assertEqual(self.broker.submissions, 2)

    def test_invalid_signal_cannot_close_position(self):
        self.tick(self.long)
        for signal in (replace(self.short, expires=self.now), replace(self.short, version="old"),
                       replace(self.short, created=self.now+timedelta(seconds=1)),
                       replace(self.short, stop=D("NaN")), replace(self.short, stop=D(1990)),
                       replace(self.short, direction=Direction.NO_TRADE)):
            self.tick(signal)
            self.assertIsNotNone(self.broker.position)
        self.assertEqual(self.exit_reasons(), [])

    def test_missing_analysis_or_data_or_clock_blocks_reverse(self):
        self.tick(self.long)
        for quality in (replace(self.quality, analysis_available=False),
                        replace(self.quality, data_complete=False),
                        replace(self.quality, clock_offset_ms=1001)):
            self.tick(self.short, quality)
            self.assertIsNotNone(self.broker.position)

    def test_budget_exhaustion_allows_exit_not_reentry(self):
        self.tick(self.long)
        quality = replace(self.quality, budget_available=False)
        self.tick(self.short, quality)
        self.assertIsNone(self.broker.position)
        self.tick(self.short, quality)
        self.assertIsNone(self.broker.position)
        self.assertEqual(self.broker.submissions, 1)

    def test_soft_loss_allows_exit_not_reentry(self):
        self.tick(self.long)
        self.engine.realized = D(-300)
        self.tick(self.short)
        self.assertIsNone(self.broker.position)
        self.tick(self.short)
        self.assertEqual(self.broker.submissions, 1)

    def test_stop_has_priority_over_reverse(self):
        self.tick(self.long)
        self.tick(self.short, quote=Quote(self.now, D(1988), D("1988.5")))
        self.assertEqual(self.exit_reasons(), ["STOP"])
        self.assertNotIn("reverse:short", [r[0] for r in self.store.db.execute("SELECT id FROM intents")])

    def test_reverse_loss_can_trigger_hard_lock(self):
        self.tick(self.long)
        self.engine.realized = D("-999.9")
        self.tick(self.short)
        self.assertEqual(self.engine.state, "HARD_LOCKED")
        self.tick(self.short)
        self.assertEqual(self.broker.submissions, 1)

    def test_close_timeout_after_fill_never_blindly_retries(self):
        self.tick(self.long)
        close = self.broker.close
        def uncertain(quote):
            close(quote)
            raise TimeoutError("private details")
        self.broker.close = Mock(side_effect=uncertain)
        self.tick(self.short)
        self.assertEqual(self.engine.state, "RECOVERY_LOCKED")
        self.assertIn("reverse:short", self.store.unresolved_intents())
        self.tick(self.short)
        self.broker.close.assert_called_once()
        self.assertNotIn("private details", str(self.store.events()))

    def test_close_timeout_before_fill_does_not_repeat_reverse(self):
        self.tick(self.long)
        self.broker.close = Mock(side_effect=TimeoutError())
        self.tick(self.short)
        self.assertIsNotNone(self.broker.position)
        self.assertEqual(self.engine.state, "RECOVERY_LOCKED")
        self.tick(self.short)
        self.broker.close.assert_called_once()

    def test_session_exit_has_priority_over_reverse(self):
        self.tick(self.long)
        self.tick(self.short, replace(self.quality, minutes_to_boundary=D(10)))
        self.assertEqual(self.exit_reasons(), ["SESSION_CLOSE"])
        self.assertEqual(self.broker.submissions, 1)

    def test_duplicate_reverse_does_not_skip_partial_management(self):
        self.tick(self.long)
        self.store.claim("reverse:short")
        signal = replace(self.short, stop=D(2050))
        self.tick(signal, quote=Quote(self.now, D(2012), D("2012.5")))
        self.assertTrue(self.broker.position.partial_taken)
        self.assertEqual(self.exit_reasons(), ["ONE_R_HALF"])
