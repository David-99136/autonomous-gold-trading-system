import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock

from gold_system.core import D, Signal, Direction, Mode
from gold_system.costs import CostLedger, BudgetRejected, AnalysisReceipt, budgeted_analysis
from gold_system.store import Store


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"cost.db"
        self.store = Store(self.path)
        self.ledger = CostLedger(self.store)
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.signal = Signal("s1", "test", self.now, self.now+timedelta(minutes=1),
                             Direction.NO_TRADE, Mode.RIGHT, D(0), D(0), "test")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def configure(self, cap="1"):
        self.ledger.configure_month(self.now, D(cap))

    def reserve(self, request="r1", maximum="0.6", category="AI"):
        return self.ledger.reserve(request, category, D(maximum), self.now)

    def status(self):
        return self.ledger.status(self.now)

    def test_unconfigured_is_not_zero_cost_or_unlimited(self):
        status = self.status()
        self.assertIsNone(status["cap_usd"])
        self.assertIsNone(status["remaining_usd"])
        self.assertFalse(status["analysis_available"])
        with self.assertRaises(BudgetRejected):
            self.reserve()

    def test_reservation_settlement_and_idempotency(self):
        self.configure()
        self.assertTrue(self.reserve())
        self.assertFalse(self.reserve())
        self.assertEqual(self.status()["remaining_usd"], D("0.4"))
        with self.assertRaises(BudgetRejected):
            self.reserve("r2")
        self.assertEqual(self.ledger.settle("r1", D("0.2")), "SETTLED")
        self.ledger.settle("r1", D("0.2"))
        self.assertEqual(self.status()["remaining_usd"], D("0.8"))
        self.assertEqual(self.status()["spent_usd"], D("0.2"))
        self.assertEqual(self.status()["reserved_usd"], D(0))
        with self.assertRaises(BudgetRejected):
            self.ledger.settle("r1", D("0.1"))

    def test_unknown_holds_reserve_and_requires_actual_settlement(self):
        self.configure()
        self.reserve()
        self.ledger.mark_unknown("r1")
        self.ledger.mark_unknown("r1")
        self.assertEqual(self.status()["reserved_usd"], D("0.6"))
        self.assertFalse(self.status()["analysis_available"])
        with self.assertRaises(BudgetRejected):
            self.reserve("r2", "0.1")
        self.ledger.settle("r1", D("0.3"))
        self.assertTrue(self.status()["analysis_available"])

    def test_overspend_is_recorded_not_clamped_and_latches(self):
        self.configure()
        self.reserve()
        self.assertEqual(self.ledger.settle("r1", D("1.2")), "OVERRUN")
        self.assertEqual(self.status()["spent_usd"], D("1.2"))
        self.assertEqual(self.status()["remaining_usd"], 0)
        self.assertTrue(self.status()["overrun"])
        self.ledger.mark_unknown("r1")
        self.assertTrue(self.status()["overrun"])

    def test_reservation_overrun_locks_even_with_monthly_room(self):
        self.configure("100")
        self.reserve()
        self.ledger.settle("r1", D("0.61"))
        self.assertFalse(self.status()["analysis_available"])

    def test_restart_does_not_release_unfinished_reservation(self):
        self.configure()
        self.reserve()
        self.store.close()
        self.store = Store(self.path)
        self.ledger = CostLedger(self.store)
        self.assertEqual(self.status()["reserved_usd"], D("0.6"))
        self.assertFalse(self.reserve())

    def test_month_utc_boundary_requires_new_explicit_cap(self):
        self.configure()
        self.reserve()
        # 10/1 台北早上 7 點仍是 9 月 UTC，使用相同額度。
        early = datetime(2026, 10, 1, 7, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(self.ledger.status(early)["remaining_usd"], D("0.4"))
        new = early+timedelta(hours=1)
        self.assertFalse(self.ledger.status(new)["analysis_available"])
        self.ledger.configure_month(new, D(1))
        # 跨月入帳仍歸到原請求月份，不把舊支出藏到新月。
        self.ledger.settle("r1", D("0.5"))
        self.assertEqual(self.status()["spent_usd"], D("0.5"))
        self.assertEqual(self.ledger.status(new)["spent_usd"], 0)

    def test_all_operating_categories_share_cap(self):
        self.configure()
        self.reserve("data", "0.4", "DATA")
        self.reserve("host", "0.4", "HOSTING")
        with self.assertRaises(BudgetRejected):
            self.reserve("ai", "0.3")

    def test_cap_and_request_conflicts_fail_closed(self):
        self.configure()
        self.configure()
        with self.assertRaises(BudgetRejected):
            self.configure("2")
        self.reserve()
        with self.assertRaises(BudgetRejected):
            self.reserve(maximum="0.5")
        with self.assertRaises(BudgetRejected):
            self.reserve(category="HOSTING")
        self.assertEqual(self.status()["cap_usd"], 1)

    def test_invalid_amounts_time_and_id(self):
        for value in (D("NaN"), D("Infinity"), D(-1), D(0), 0.1, True):
            with self.assertRaises(ValueError):
                self.ledger.configure_month(self.now, value)
        with self.assertRaises(ValueError):
            self.ledger.configure_month(self.now.replace(tzinfo=None), D(1))
        self.configure()
        with self.assertRaises(ValueError):
            self.reserve("email@example.com")
        with self.assertRaises(ValueError):
            self.reserve(category="OTHER")

    def test_two_connections_cannot_oversubscribe(self):
        self.configure()
        barrier = Barrier(2)
        def worker(request):
            store = Store(self.path)
            try:
                ledger = CostLedger(store)
                barrier.wait(timeout=5)
                try:
                    return ledger.reserve(request, "AI", D("0.6"), self.now)
                except BudgetRejected:
                    return False
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ["a", "b"]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.status()["reserved_usd"], D("0.6"))

    def test_provider_not_called_without_budget_or_for_duplicate(self):
        provider = AsyncMock(return_value=AnalysisReceipt(self.signal, D("0.1")))
        async def run():
            return await budgeted_analysis(self.ledger, "req", D("0.5"), self.now, provider)
        with self.assertRaises(BudgetRejected):
            asyncio.run(run())
        provider.assert_not_called()
        self.configure()
        self.assertEqual(asyncio.run(run()), self.signal)
        with self.assertRaises(BudgetRejected):
            asyncio.run(run())
        provider.assert_awaited_once()

    def test_provider_timeout_and_cancellation_hold_budget(self):
        self.configure("10")
        for i, error in enumerate((TimeoutError("private-provider-error"), asyncio.CancelledError())):
            provider = AsyncMock(side_effect=error)
            expected_error = RuntimeError if isinstance(error, TimeoutError) else asyncio.CancelledError
            with self.assertRaises(expected_error) as caught:
                asyncio.run(budgeted_analysis(self.ledger, f"req{i}", D(1), self.now, provider))
            self.assertNotIn("private-provider-error", str(caught.exception))
            self.assertEqual(self.status()["unknown_requests"], 1)
            self.assertNotIn("private-provider-error", str(self.store.events()))
            self.ledger.settle(f"req{i}", D("0.5"))

    def test_provider_cost_overrun_does_not_return_signal(self):
        self.configure()
        provider = AsyncMock(return_value=AnalysisReceipt(self.signal, D("0.9")))
        with self.assertRaises(BudgetRejected):
            asyncio.run(budgeted_analysis(self.ledger, "req", D("0.5"), self.now, provider))
        self.assertEqual(self.status()["spent_usd"], D("0.9"))
        self.assertTrue(self.status()["overrun"])
