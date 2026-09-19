import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gold_system.broker import PaperBroker
from gold_system.core import Bar, Contract, D, Direction, Mode, Quality, Quote, RiskPolicy, Signal, Rejected
from gold_system.engine import Engine
from gold_system.risk import make_plan, make_add_plan
from gold_system.store import Store


class PyramidingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 14, tzinfo=timezone.utc)
        self.policy = RiskPolicy()
        self.contract = Contract("TEST", D(1), D("0.01"), D("0.01"), D(100), D("0.01"), D("0.1"))
        self.quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        self.quote = Quote(self.now, D(2000), D("2000.5"))
        self.signal = Signal("initial", self.policy.version, self.now, self.now+timedelta(minutes=1),
                             Direction.LONG, Mode.RIGHT, D(1990), D(2050), "test")
        self.broker = PaperBroker()

    def prepare(self, direction=Direction.LONG):
        if direction == Direction.SHORT:
            self.signal = replace(self.signal, direction=direction, stop=D(2010), target=D(1950))
        plan = make_plan(self.signal, self.quote, self.quality, self.contract, self.policy, self.broker.balance, self.now)
        self.broker.open(plan, self.contract)
        p = self.broker.position
        self.broker.tighten(p.entry + self.policy.slippage * direction.sign)
        now = self.now+timedelta(minutes=5)
        q = Quote(now, D(2012) if direction == Direction.LONG else D(1988),
                       D("2012.5") if direction == Direction.LONG else D("1988.5"))
        signal = replace(self.signal, signal_id="add", created=now, expires=now+timedelta(minutes=1), stop=p.stop)
        bar = Bar(now, D(2005), D(2012), D(2004), D(2011)) if direction == Direction.LONG else Bar(now, D(1995), D(1996), D(1988), D(1989))
        return p, now, q, signal, bar

    def add_plan(self, p, now, q, signal, bar, equity=D(10000), quality=None):
        return make_add_plan(signal, q, quality or self.quality, self.contract, self.policy, equity, now, p, bar)

    def test_two_sided_stop_slippage_equals_pretrade_risk(self):
        for direction in (Direction.LONG, Direction.SHORT):
            broker = PaperBroker()
            signal = self.signal if direction == Direction.LONG else replace(self.signal, direction=direction, stop=D(2010), target=D(1950))
            plan = make_plan(signal, self.quote, self.quality, self.contract, self.policy, broker.balance, self.now)
            broker.open(plan, self.contract)
            quote = Quote(self.now, signal.stop, signal.stop+D("0.5")) if direction == Direction.LONG else Quote(self.now, signal.stop-D("0.5"), signal.stop)
            self.assertEqual(-broker.close(quote), plan.risk)
            self.assertLessEqual(plan.risk, D(25))

    def test_both_directions_preserve_original_risk_and_total_margin(self):
        for direction in (Direction.LONG, Direction.SHORT):
            self.broker = PaperBroker()
            p, now, q, signal, bar = self.prepare(direction)
            initial_entry, original_risk = p.initial_entry, p.original_risk
            plan = self.add_plan(p, now, q, signal, bar)
            self.broker.add(plan)
            remaining_loss = max(D(0), (p.entry-p.stop)*p.direction.sign+self.policy.slippage)*p.size
            self.assertLessEqual(remaining_loss, original_risk + D("1e-20"))
            self.assertLessEqual(p.size*(plan.entry+self.policy.slippage)*self.contract.margin_rate, D(2000))
            self.assertEqual(p.initial_entry, initial_entry)
            self.assertTrue(p.added)
            with self.assertRaises(Rejected):
                self.add_plan(p, now, q, replace(signal, signal_id="again"), bar)

    def test_no_averaging_down_or_unprotected_add(self):
        p, now, q, signal, bar = self.prepare()
        with self.assertRaises(Rejected):
            self.add_plan(p, now, Quote(now, D(1999), D("1999.5")), signal, bar)
        p.stop = D(1990)
        with self.assertRaises(Rejected):
            self.add_plan(p, now, q, signal, bar)

    def test_confirmation_must_be_new_closed_aligned_and_directional(self):
        p, now, q, signal, bar = self.prepare()
        for bad in (None, replace(bar, timestamp=self.now), replace(bar, timestamp=now+timedelta(minutes=5)),
                    replace(bar, timestamp=now-timedelta(minutes=1)), replace(bar, close=bar.open)):
            with self.assertRaises(Rejected):
                self.add_plan(p, now, q, signal, bad)

    def test_add_obeys_entry_quality_and_mode(self):
        p, now, q, signal, bar = self.prepare()
        for quality in (replace(self.quality, budget_available=False), replace(self.quality, extreme_volatility=True),
                        replace(self.quality, minutes_to_boundary=D(30))):
            with self.assertRaises(Rejected):
                self.add_plan(p, now, q, signal, bar, quality=quality)
        with self.assertRaises(Rejected):
            self.add_plan(p, now, q, replace(signal, mode=Mode.LEFT), bar)
        with self.assertRaises(Rejected):
            self.add_plan(p, now, q, replace(signal, stop=p.stop-D(1)), bar)

    def test_no_margin_room_rejects_instead_of_ignoring_existing_position(self):
        p, now, q, signal, bar = self.prepare()
        with self.assertRaises(Rejected):
            self.add_plan(p, now, q, signal, bar, equity=D(100))

    def test_protected_add_before_half_exit_keeps_even_size(self):
        p, now, q, signal, bar = self.prepare()
        q = Quote(now, D(2008), D("2008.5"))
        self.assertFalse(p.partial_taken)
        plan = self.add_plan(p, now, q, signal, bar)
        self.assertEqual(plan.size % (2*self.contract.increment), 0)
        self.assertGreater(plan.size, 0)

    def test_slippage_model_mismatch_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"audit.db")
            try:
                with self.assertRaises(ValueError):
                    Engine(store, PaperBroker(slippage=D(1)), self.contract, self.policy)
            finally:
                store.close()

    def test_engine_partial_then_add_once_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"audit.db")
            try:
                engine = Engine(store, self.broker, self.contract, self.policy)
                asyncio.run(engine.tick(self.now, self.quote, self.quality, self.signal))
                now = self.now+timedelta(minutes=5)
                q = Quote(now, D(2012), D("2012.5"))
                bar = Bar(now, D(2005), D(2012), D(2004), D(2011))
                signal = replace(self.signal, signal_id="addon", created=now, expires=now+timedelta(minutes=1), stop=D("2000.7"))
                asyncio.run(engine.tick(now, q, self.quality, signal, add_confirmation=bar))
                self.assertTrue(self.broker.position.partial_taken)
                self.assertTrue(self.broker.position.added)
                self.assertEqual(self.broker.submissions, 2)
                self.assertIn("ADD_ON", [e["kind"] for e in store.events()])
                asyncio.run(engine.tick(now, q, self.quality, replace(signal, signal_id="third"), add_confirmation=bar))
                self.assertEqual(self.broker.submissions, 2)
            finally:
                store.close()
