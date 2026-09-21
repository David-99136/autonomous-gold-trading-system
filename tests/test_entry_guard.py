import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from gold_system.async_capital import AsyncCapitalDemo
from gold_system.core import D, Contract, Direction, Mode, Quote, Quality, RiskPolicy, Signal
from gold_system.costs import CostLedger
from gold_system.entry_guard import DemoEntryGuard, EntryContext
from gold_system.order_journal import OrderJournal
from gold_system.reconciliation import Snapshot, reconcile_open_async
from gold_system.risk import make_plan
from gold_system.store import Store


class EntryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"guard.db")
        self.journal = OrderJournal(self.store)
        self.costs = CostLedger(self.store)
        self.now = datetime.now(timezone.utc)
        self.account_hash = sha256(b"account").hexdigest()
        self.policy = RiskPolicy()
        contract = Contract("GOLD", D(1), D("0.01"), D("0.01"), D(100), D("0.05"), D("0.05"))
        quote = Quote(self.now, D(2000), D("2000.5"))
        quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        signal = Signal("one", self.policy.version, self.now, self.now+timedelta(minutes=1),
                        Direction.LONG, Mode.RIGHT, D(1990), D(2030), "test")
        plan = make_plan(signal, quote, quality, contract, self.policy, D(10000), self.now)
        self.journal.prepare(plan, "GOLD", self.account_hash)
        self.journal.transition("one", "SUBMITTING")
        snapshot = Snapshot({"accountId": "account", "currency": "USD"}, {"accountId": "account", "currency": "USD"},
                            {"hedgingMode": False}, {"positions": []}, {"workingOrders": []},
                            {"activities": []}, self.now, 1)
        self.context = EntryContext(snapshot, quote, quality, contract, D(10000), D(10000), D(0),
                                   self.now.date().isoformat(), self.now, self.now, D("0.01"), D(2000),
                                   True, True, True, False, False)
        self.source = AsyncMock(return_value=self.context)
        self.guard = DemoEntryGuard(self.source, self.costs, self.store, self.account_hash, clock=lambda: self.now)
        self.costs.configure_month(self.now, D(1))  # 只在臨時測試資料庫設定，不影響使用者月額。

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def check(self, **changes):
        self.source.return_value = replace(self.context, **changes)
        return await self.guard(self.journal, "one")

    async def test_all_independent_checks_required(self):
        self.assertTrue(await self.check())
        for changes in ({"demo_enabled": False}, {"strategy_validated": False}, {"contract_verified": False},
                        {"soft_locked": True}, {"hard_locked": True}):
            self.assertFalse(await self.check(**changes))

    async def test_realized_loss_gate_and_day_freshness(self):
        self.assertFalse(await self.check(realized_daily_pnl=D(-300)))
        self.assertFalse(await self.check(realized_daily_pnl=D(-1000)))
        self.assertFalse(await self.check(accounting_day="2020-01-01"))
        self.assertFalse(await self.check(accounting_received_at=self.now-timedelta(seconds=3)))
        self.assertFalse(await self.check(contract_received_at=self.now-timedelta(minutes=6)))

    async def test_unknown_old_intent_or_broker_working_order_blocks(self):
        snapshot = replace(self.context.snapshot, orders={"workingOrders": [{}]})
        self.assertFalse(await self.check(snapshot=snapshot))
        self.store.claim("unknown-old-intent")
        self.assertFalse(await self.check())

    async def test_actual_cost_ledger_exhaustion_blocks(self):
        self.costs.reserve("expense", "AI", D(1), self.now)
        self.assertFalse(await self.check())

    async def test_operator_stop_is_independent_of_source_approval(self):
        self.store.stop_new_entries()
        self.assertFalse(await self.check())

    async def test_history_quality_fault_blocks_demo_entry_guard(self):
        from gold_system.history_quality import HistoryQualityMonitor
        boundary = self.now.replace(second=0, microsecond=0)
        HistoryQualityMonitor(self.store).observe({}, start=boundary, end=boundary, received_at=self.now)
        self.assertFalse(await self.check())

    async def test_equity_reduction_reprices_size_not_old_approval(self):
        self.assertFalse(await self.check(equity=D(100)))
        self.assertFalse(await self.check(equity=D("NaN")))

    async def test_stale_quote_and_market_quality_still_enforced(self):
        self.assertFalse(await self.check(quote=replace(self.context.quote, timestamp=self.now-timedelta(seconds=3))))
        self.assertFalse(await self.check(quality=replace(self.context.quality, extreme_volatility=True)))
        self.assertFalse(await self.check(quality=replace(self.context.quality, minutes_to_boundary=D(30))))

    async def test_price_rules_and_changed_entry_block(self):
        self.assertFalse(await self.check(maximum_stop_distance=D(1)))
        self.assertFalse(await self.check(price_tick=D(3)))
        self.assertFalse(await self.check(quote=Quote(self.now, D(2001), D("2001.5"))))

    async def test_source_failure_is_not_approved_or_logged_raw(self):
        self.source.side_effect = RuntimeError("private credentials")
        self.assertFalse(await self.guard(self.journal, "one"))
        self.assertNotIn("private credentials", str(self.store.events()))

    def test_policy_cannot_raise_user_risk_limits(self):
        for policy in (replace(self.policy, risk_fraction=D("0.01")), replace(self.policy, normal_margin=D("0.7")),
                       replace(self.policy, soft_loss=D("0.05")), replace(self.policy, minimum_rr=D(1))):
            with self.assertRaises(ValueError):
                DemoEntryGuard(self.source, self.costs, self.store, self.account_hash, policy)

    async def exercise_gateway(self, *, missing_stop=False, reject_guard=False):
        """真實 guard / HTTP adapter / reconciler 接線；只有遠端 HTTP 是假資料。"""
        requests = []
        request_data = self.journal.get("one")["request"]
        position = {"dealId": "p1", "direction": "BUY", "currency": "USD",
                    "size": request_data["size"], "level": request_data["planned_entry"]}
        if not missing_stop:
            position["stopLevel"] = request_data["stopLevel"]

        async def handle(request):
            requests.append(request)
            path = request.url.path
            if path.endswith("/session"):
                return httpx.Response(200, json={"accountId": "account", "currency": "USD"},
                                      headers={"CST": "fixture-cst", "X-SECURITY-TOKEN": "fixture-token"})
            if path.endswith("/positions") and request.method == "POST":
                return httpx.Response(200, json={"dealReference": "open1"})
            if path.endswith("/confirms/open1"):
                return httpx.Response(200, json={"dealReference": "open1", "dealStatus": "ACCEPTED",
                    "status": "OPEN", "epic": "GOLD", "direction": "BUY", "size": position["size"],
                    "level": position["level"], "affectedDeals": [{"dealId": "p1", "status": "OPENED"}]})
            responses = {"/api/v1/accounts/preferences": {"hedgingMode": False},
                         "/api/v1/positions": {"positions": [{"position": position, "market": {"epic": "GOLD"}}]},
                         "/api/v1/workingorders": {"workingOrders": []},
                         "/api/v1/history/activity": {"activities": []}}
            # 不明 endpoint 必須使測試失敗，不能以空成功回應掩蓋接線錯誤。
            return httpx.Response(200, json=responses[path])

        client = AsyncCapitalDemo(transport=httpx.MockTransport(handle), write_guard=self.guard)
        try:
            await client.login({"identifier": "fixture", "api_key": "fixture", "password": "fixture"})
            if reject_guard:
                self.source.return_value = replace(self.context, soft_locked=True)
                from gold_system.core import Rejected
                with self.assertRaisesRegex(Rejected, "GUARD_REJECTED"):
                    await client.post_prepared_open(self.journal, "one")
                self.assertFalse(any(r.url.path.endswith("/positions") for r in requests))
                return
            reply = await client.post_prepared_open(self.journal, "one")
            self.journal.transition("one", "ACKNOWLEDGED", reference=reply["dealReference"])
            result = await reconcile_open_async(client, self.journal, "one", self.context.contract, self.policy)
            self.journal.transition("one", result["outcome"])
            self.assertEqual(sum(r.method == "POST" and r.url.path.endswith("/positions") for r in requests), 1)
            self.assertEqual(result["outcome"], "UNKNOWN" if missing_stop else "CONFIRMED")
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(self.journal.unresolved(), ["one"] if missing_stop else [])
        finally:
            await client.close()

    async def test_real_guard_http_and_reconciler_accept_matching_fill(self):
        await self.exercise_gateway()

    async def test_real_guard_http_and_reconciler_lock_missing_stop(self):
        await self.exercise_gateway(missing_stop=True)

    async def test_real_guard_blocks_http_post(self):
        await self.exercise_gateway(reject_guard=True)
