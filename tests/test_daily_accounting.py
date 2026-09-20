import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

from gold_system.accounting import AccountingLedger
from gold_system.daily_control import DailyControl
from gold_system.settlement import SettlementLedger
from gold_system.store import Store


class DailyAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "events.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.settlements = SettlementLedger(self.store)
        self.accounting = AccountingLedger(self.settlements.db)
        self.now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        self.start = "2026-09-20T00:00:00+00:00"
        self.end = self.now.isoformat()
        self.account = "a" * 64
        self.args = dict(mode="DEMO", account_hash=self.account, start=self.start, end=self.end)
        self.control = DailyControl(self.accounting, session_boundary=asyncio.Lock(), clock=lambda: self.now)

    def close_segment(self, pnl="-1000", fee="0", *, fill="b", mode="DEMO", account=None, at=None):
        return self.settlements.record_close(mode=mode, account_hash=account or self.account,
            fill_hash=fill * 64, trade_hash="c" * 64, evidence_hash="d" * 64,
            occurred_at=at or self.now - timedelta(minutes=1), quantity=D("0.001"),
            realized_pnl=D(pnl), extra_fee=None if fee is None else D(fee))

    def verify(self, net="-1000", fills=("b",), equity="10000", **overrides):
        return self.accounting.reconcile_period(**(self.args | overrides), opening_equity=D(equity),
            expected_fill_hashes=[f * 64 for f in fills], broker_net_pnl=D(net), evidence_hash="e" * 64)

    def observe(self, **overrides):
        return self.control.observe(**(dict(account_hash=self.account, start=self.start, end=self.end) | overrides))

    def flat(self, **changes):
        return dict(state="SNAPSHOT_MATCHED", account_hash=self.account, reasons=[], position_count=0,
                    working_order_count=0, received_at=self.now.isoformat(), elapsed_seconds=0.1,
                    entries_enabled=False) | changes

    async def run_hard(self, **overrides):
        actions = dict(reduce_and_verify=AsyncMock(return_value=self.flat()),
                       reauthenticate=AsyncMock(), reconcile=AsyncMock(return_value=self.flat())) | overrides
        result = await self.control.reconcile_hard_limit(account_hash=self.account, start=self.start, **actions)
        return result, actions

    def test_exact_fees_and_partial_segment_aggregation(self):
        self.close_segment("1.20", "0.10")
        self.close_segment("-0.20", "0.05", fill="f")
        self.verify(net="0.85", fills=("b", "f"))
        result = self.accounting.verified_period(**self.args)
        self.assertEqual(D(result["net_pnl"]), D("0.85"))
        self.assertEqual(result["segment_count"], 2)
        self.assertNotIn("win_rate", result)

    def test_unknown_fee_blocks_until_evidence_is_recorded(self):
        key = self.close_segment(fee=None)
        with self.assertRaisesRegex(ValueError, "STATEMENT_MISMATCH"):
            self.verify()
        self.accounting.record_fee(key, D("0.25"), "f" * 64)
        self.verify(net="-1000.25")
        with self.assertRaisesRegex(ValueError, "FEE_CONFLICT"):
            self.accounting.record_fee(key, D("0"), "f" * 64)

    def test_empty_book_requires_explicit_authoritative_zero_receipt(self):
        with self.assertRaisesRegex(ValueError, "RECEIPT_MISSING"):
            self.accounting.verified_period(**self.args)
        self.verify(net="0", fills=())
        self.assertEqual(self.accounting.verified_period(**self.args)["net_pnl"], "0")

    def test_manifest_and_net_mismatch_rejected(self):
        self.close_segment()
        for data in (dict(fills=()), dict(net="0"), dict(fills=("b", "b"))):
            with self.assertRaises(ValueError):
                self.verify(**data)

    def test_late_fill_invalidates_receipt(self):
        self.close_segment()
        self.verify()
        self.close_segment("-1", fill="f")
        with self.assertRaisesRegex(ValueError, "RECEIPT_STALE"):
            self.accounting.verified_period(**self.args)
        self.assertEqual(self.observe()["phase"], "ACCOUNTING_UNVERIFIED")
        self.assertTrue(self.store.entries_stopped())

    def test_fee_evidence_update_invalidates_receipt(self):
        key = self.close_segment()
        self.verify()
        self.accounting.record_fee(key, D(0), "f" * 64)
        with self.assertRaisesRegex(ValueError, "RECEIPT_STALE"):
            self.accounting.verified_period(**self.args)

    def test_modes_accounts_and_interval_end_are_separated(self):
        self.close_segment("-5000", mode="PAPER")
        self.close_segment("-5000", account="f" * 64)
        self.close_segment("-5000", at=self.now)  # 右端點不計入。
        self.verify(net="0", fills=())
        self.assertEqual(self.observe()["phase"], "MONITORING")

    def test_opening_equity_is_frozen(self):
        self.close_segment()
        self.verify()
        with self.assertRaisesRegex(ValueError, "OPENING_EQUITY_CONFLICT"):
            self.verify(equity="20000")

    def test_unverified_or_stale_accounting_blocks_entries(self):
        self.assertEqual(self.observe()["phase"], "ACCOUNTING_UNVERIFIED")
        self.assertTrue(self.store.entries_stopped())
        self.close_segment()
        self.verify()
        self.now += timedelta(seconds=3)
        self.assertEqual(self.observe()["phase"], "ACCOUNTING_UNVERIFIED")

    async def test_below_soft_has_no_callbacks_or_entry_unlock(self):
        self.close_segment("-299.99")
        self.verify(net="-299.99")
        self.assertEqual(self.observe()["phase"], "MONITORING")
        result, actions = await self.run_hard()
        self.assertEqual(result["phase"], "NOT_CLAIMED")
        for action in actions.values():
            action.assert_not_awaited()

    async def test_soft_at_three_percent_stops_entries_without_relogin(self):
        self.close_segment("-300")
        self.verify(net="-300")
        self.assertEqual(self.observe()["phase"], "SOFT_LOCKED")
        self.assertTrue(self.store.entries_stopped())
        _, actions = await self.run_hard()
        actions["reauthenticate"].assert_not_awaited()

    async def test_hard_at_ten_percent_orders_callbacks_and_runs_once(self):
        self.close_segment()
        self.verify()
        self.assertEqual(self.observe()["phase"], "PENDING_REDUCTION")
        sequence = []

        async def reduce():
            self.assertTrue(self.store.entries_stopped())
            sequence.append("reduce")
            return self.flat()

        async def login():
            sequence.append("login")

        async def reconcile():
            sequence.append("reconcile")
            return self.flat()

        result, _ = await self.run_hard(reduce_and_verify=reduce, reauthenticate=login, reconcile=reconcile)
        self.assertEqual(sequence, ["reduce", "login", "reconcile"])
        self.assertEqual(result["phase"], "COMPLETE_HARD_LOCKED")
        self.assertTrue(self.store.entries_stopped())
        self.observe()
        result, actions = await self.run_hard()
        self.assertEqual(result["phase"], "NOT_CLAIMED")
        actions["reauthenticate"].assert_not_awaited()

    async def test_failed_risk_reduction_never_reauthenticates(self):
        self.close_segment()
        self.verify()
        self.observe()
        result, actions = await self.run_hard(reduce_and_verify=AsyncMock(return_value=self.flat(position_count=1)))
        self.assertEqual(result["phase"], "RECOVERY_REQUIRED")
        actions["reauthenticate"].assert_not_awaited()

    async def test_reauth_failure_is_safe_and_not_retried(self):
        self.close_segment()
        self.verify()
        self.observe()
        result, actions = await self.run_hard(reauthenticate=AsyncMock(side_effect=RuntimeError("secret-canary")))
        self.assertEqual(result["phase"], "RECOVERY_REQUIRED")
        actions["reconcile"].assert_not_awaited()
        self.assertNotIn("secret-canary", repr(self.store.events()))
        _, retry = await self.run_hard()
        retry["reauthenticate"].assert_not_awaited()

    async def test_new_session_wrong_account_or_unresolved_state_does_not_complete(self):
        self.close_segment()
        self.verify()
        self.observe()
        result, _ = await self.run_hard(reconcile=AsyncMock(return_value=self.flat(account_hash="f" * 64)))
        self.assertEqual(result["phase"], "RECOVERY_REQUIRED")

    async def test_concurrent_workers_only_one_claim(self):
        self.close_segment()
        self.verify()
        self.observe()
        other = Store(self.path)
        try:
            control = DailyControl(AccountingLedger(SettlementLedger(other).db),
                                   session_boundary=self.control.session_boundary, clock=lambda: self.now)
            reduce = AsyncMock(return_value=self.flat())
            login = AsyncMock()
            reconcile = AsyncMock(return_value=self.flat())
            args = dict(account_hash=self.account, start=self.start, reduce_and_verify=reduce,
                        reauthenticate=login, reconcile=reconcile)
            results = await asyncio.gather(self.control.reconcile_hard_limit(**args), control.reconcile_hard_limit(**args))
            self.assertEqual(sorted(x["phase"] for x in results), ["COMPLETE_HARD_LOCKED", "NOT_CLAIMED"])
            login.assert_awaited_once()
        finally:
            other.close()

    async def test_restart_does_not_repeat_inflight_reduction(self):
        self.close_segment()
        self.verify()
        self.observe()
        self.control._transition(self.account, self.start, "PENDING_REDUCTION", "REDUCING")
        resumed = DailyControl(self.accounting, session_boundary=asyncio.Lock(), clock=lambda: self.now)
        self.control = resumed
        result, actions = await self.run_hard()
        self.assertEqual(result["phase"], "NOT_CLAIMED")
        actions["reduce_and_verify"].assert_not_awaited()

    async def test_cancellation_leaves_recovery_required(self):
        self.close_segment()
        self.verify()
        self.observe()
        started = asyncio.Event()

        async def slow_reduce():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.run_hard(reduce_and_verify=slow_reduce))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.store.db.execute("SELECT phase FROM daily_control").fetchone()
        self.assertEqual(row[0], "RECOVERY_REQUIRED")

    async def test_hard_lock_survives_day_change_and_does_not_relogin_again(self):
        self.close_segment()
        self.verify()
        self.observe()
        await self.run_hard()
        self.now += timedelta(days=1)
        self.start, self.end = "2026-09-21T00:00:00+00:00", self.now.isoformat()
        self.args.update(start=self.start, end=self.end)
        self.verify(net="0", fills=())
        self.assertEqual(self.observe()["phase"], "INHERITED_HARD_LOCK")
        _, actions = await self.run_hard()
        actions["reauthenticate"].assert_not_awaited()

    def test_operator_stop_is_never_cleared(self):
        self.store.stop_new_entries()
        self.verify(net="0", fills=())
        self.observe()
        self.assertTrue(self.store.entries_stopped())

    def test_invalid_daily_lock_state_fails_closed(self):
        self.store.set("daily_new_entries_blocked", "false")
        self.assertTrue(self.store.entries_stopped())

    def test_report_reads_verified_totals_without_mutating_database(self):
        from gold_system.reporting import summarize
        self.close_segment()
        self.verify()
        report = summarize(self.path, self.start, self.end, "DEMO", account_hash=self.account)
        self.assertEqual(report["accounting"]["status"], "MATCHED")
        self.assertEqual(D(report["accounting"]["realized_net_pnl"]), D(-1000))
        self.assertNotIn(self.account, repr(report))
        self.close_segment("-1", fill="f")
        report = summarize(self.path, self.start, self.end, "DEMO", account_hash=self.account)
        self.assertEqual(report["accounting"]["status"], "UNVERIFIED")
        self.assertIsNone(report["accounting"]["realized_net_pnl"])

    def test_soft_lock_is_sticky_after_intraday_recovery(self):
        self.close_segment("-300")
        self.verify(net="-300")
        self.observe()
        self.close_segment("400", fill="f")
        self.verify(net="100", fills=("b", "f"))
        self.assertEqual(self.observe()["phase"], "SOFT_LOCKED")

    async def test_boolean_flat_snapshot_blocks_relogin(self):
        self.close_segment()
        self.verify()
        self.observe()
        result, actions = await self.run_hard(reduce_and_verify=AsyncMock(return_value=self.flat(position_count=False)))
        self.assertEqual(result["phase"], "RECOVERY_REQUIRED")
        actions["reauthenticate"].assert_not_awaited()

    def test_cutoff_cannot_move_backwards(self):
        self.close_segment()
        self.verify()
        self.observe()
        older = (self.now - timedelta(seconds=1)).isoformat()
        self.verify(end=older)
        self.assertEqual(self.observe(end=older)["phase"], "ACCOUNTING_UNVERIFIED")
