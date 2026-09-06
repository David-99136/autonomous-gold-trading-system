import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from gold_system.core import *
from gold_system.risk import make_plan
from gold_system.broker import PaperBroker
from gold_system.engine import Engine
from gold_system.store import Store
from gold_system.analysis import evidence_allowed, analyze, continuous


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "audit.db")
        self.now = datetime(2026, 1, 5, 14, tzinfo=timezone.utc)
        self.policy = RiskPolicy()
        self.contract = Contract("TEST", D(1), D("0.01"), D("0.01"), D(100), D("0.01"), D("0.1"))
        self.q = Quote(self.now, D(2000), D("2000.5"))
        self.quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        self.s = Signal("one", self.policy.version, self.now, self.now + timedelta(minutes=1),
                        Direction.LONG, Mode.RIGHT, D(1990), D(2030), "test")
        self.broker = PaperBroker()
        self.engine = Engine(self.store, self.broker, self.contract, self.policy)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, signal=None, quality=None, quote=None):
        return make_plan(signal or self.s, quote or self.q, quality or self.quality,
                         self.contract, self.policy, D(10000), self.now)

    def tick(self, signal=None, quote=None, quality=None):
        asyncio.run(self.engine.tick(self.now, quote or self.q, quality or self.quality, signal))

    def test_size_respects_risk_margin_and_half_step(self):
        p = self.plan()
        self.assertLessEqual(p.risk, D(25))
        self.assertLessEqual(p.margin, D(2000))
        self.assertEqual((p.size / 2) % self.contract.increment, 0)

    def test_quality_fail_closed(self):
        cases = [dict(tradeable=False), dict(liquid_window=False), dict(data_complete=False),
                 dict(extreme_volatility=True), dict(clock_offset_ms=1001), dict(minutes_to_boundary=D(30)),
                 dict(spread_limit=D("0.1")), dict(hourly_range=D(4)), dict(analysis_available=False),
                 dict(budget_available=False)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Rejected):
                self.plan(quality=replace(self.quality, **case))

    def test_trend_exception_not_extreme_exception(self):
        self.plan(quality=replace(self.quality, hourly_range=D(4), trend_exception=True))
        with self.assertRaises(Rejected):
            self.plan(quality=replace(self.quality, extreme_volatility=True, trend_exception=True))

    def test_expiry_stale_future_and_version(self):
        for signal in (replace(self.s, expires=self.now), replace(self.s, version="changed"),
                       replace(self.s, created=self.now + timedelta(seconds=1))):
            with self.assertRaises(Rejected):
                self.plan(signal=signal)
        with self.assertRaises(Rejected):
            self.plan(quote=replace(self.q, timestamp=self.now - timedelta(seconds=3)))

    def test_invalid_stop_rr_and_nan(self):
        for s in (replace(self.s, stop=D(2001)), replace(self.s, target=D(2005)),
                  replace(self.s, stop=D("NaN"))):
            with self.assertRaises(Rejected):
                self.plan(signal=s)

    def test_short_sizing(self):
        p = self.plan(signal=replace(self.s, direction=Direction.SHORT, stop=D(2010), target=D(1970)))
        self.assertGreater(p.size, 0)

    def test_partial_and_stop_tightening(self):
        self.tick(self.s)
        original = self.broker.position.size
        self.tick(quote=Quote(self.now, D(2012), D("2012.5")))
        self.assertEqual(self.broker.position.size, original / 2)
        self.assertGreater(self.engine.realized, 0)
        with self.assertRaises(Rejected):
            self.broker.tighten(D(1990))

    def test_session_close_even_without_analysis(self):
        self.tick(self.s)
        self.tick(quality=replace(self.quality, minutes_to_boundary=D(10), analysis_available=False))
        self.assertIsNone(self.broker.position)

    def test_soft_realized_latch_and_not_floating(self):
        self.tick(self.s)
        self.assertFalse(self.engine.soft)
        self.broker.close(self.q)
        self.engine.realized = D(-300)
        self.tick(replace(self.s, signal_id="two"))
        self.assertTrue(self.engine.soft)
        self.assertEqual(self.broker.submissions, 1)
        self.engine.realized = D(0)
        self.tick(replace(self.s, signal_id="three"))
        self.assertTrue(self.engine.soft)

    def test_hard_lock_survives_restart(self):
        self.tick(self.s)
        self.engine.realized = D(-1000)
        self.tick()
        self.assertIsNone(self.broker.position)
        restarted = Engine(self.store, self.broker, self.contract, self.policy)
        self.assertTrue(restarted.hard)
        self.assertNotEqual(restarted.state, "PAPER_READY")

    def test_duplicate_after_closed(self):
        self.tick(self.s)
        self.broker.close(self.q)
        self.tick(self.s)
        self.assertEqual(self.broker.submissions, 1)

    def test_unknown_outcome_locks_without_retry(self):
        def timeout_after_open(plan, contract):
            PaperBroker.open(self.broker, plan, contract)
            raise TimeoutError()
        self.broker.open = timeout_after_open
        self.tick(self.s)
        self.tick(self.s)
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.engine.state, "RECOVERY_LOCKED")

    def test_secret_redaction(self):
        self.store.emit("TEST", nested={"api_key": "sensitive", "password": "sensitive"})
        self.assertNotIn("sensitive", json.dumps(self.store.events()))

    def test_evidence_threshold(self):
        fields = dict(source="official", released_at="t", received_at="t", actual=0, a=1, b=2, c=3)
        weights = {k: D(1) for k in "abcde"}
        self.assertFalse(evidence_allowed(fields, weights))
        fields["d"] = 4
        self.assertTrue(evidence_allowed(fields, weights))
        fields["actual"] = None
        self.assertFalse(evidence_allowed(fields, weights))

    def test_missing_analysis_no_trade(self):
        self.assertEqual(analyze([], [], [], self.now).direction, Direction.NO_TRADE)

    def test_bar_future_and_gaps(self):
        b = Bar(self.now, D(2), D(3), D(1), D(2))
        self.assertTrue(continuous([b], 5, self.now))
        self.assertFalse(continuous([replace(b, timestamp=self.now - timedelta(minutes=10)), b], 5, self.now))
        self.assertFalse(continuous([replace(b, timestamp=self.now + timedelta(minutes=1))], 5, self.now))


if __name__ == "__main__":
    unittest.main()
