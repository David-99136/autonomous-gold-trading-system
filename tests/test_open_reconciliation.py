import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock

from gold_system.core import D, Contract, Direction, Mode, Plan, RiskPolicy, Signal
from gold_system.order_journal import OrderJournal, dispatch_once
from gold_system.reconciliation import reconcile_open_async
from gold_system.store import Store


class OpenReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"journal.db")
        self.journal = OrderJournal(self.store)
        self.policy = RiskPolicy()
        self.contract = Contract("GOLD", D(1), D("0.01"), D("0.01"), D(100), D("0.05"), D("0.05"))
        now = datetime.now(timezone.utc)
        signal = Signal("one", self.policy.version, now, now+timedelta(minutes=1), Direction.LONG,
                        Mode.RIGHT, D(1990), D(2030), "fixture")
        self.plan = Plan(signal, D(2000), D("0.1"), D("1.02"), D("10.0005"))
        self.journal.prepare(self.plan, "GOLD", sha256(b"account").hexdigest())
        self.payload = {"dealReference": "o_ref", "dealStatus": "ACCEPTED", "status": "OPEN",
                        "epic": "GOLD", "direction": "BUY", "size": "0.1", "level": "2000.1",
                        "affectedDeals": [{"dealId": "p1", "status": "OPENED"}]}
        self.position = {"dealId": "p1", "direction": "BUY", "currency": "USD",
                         "size": "0.1", "level": "2000.1", "stopLevel": 1990}
        self.client = AsyncMock()
        self.client.confirmation.return_value = self.payload
        self.client.session.return_value = {"accountId": "account", "currency": "USD"}
        self.client.preferences.return_value = {"hedgingMode": False}
        self.client.positions.return_value = {"positions": [{"position": self.position, "market": {"epic": "GOLD"}}]}
        self.client.working_orders.return_value = {"workingOrders": []}
        self.client.activity.return_value = {"activities": []}

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def acknowledge(self):
        self.journal.transition("one", "SUBMITTING")
        self.journal.transition("one", "ACKNOWLEDGED", reference="o_ref")

    async def reconcile(self, reference=None):
        return await reconcile_open_async(self.client, self.journal, "one", self.contract, self.policy)

    async def test_dispatch_uses_real_reconciler_not_mock_outcome(self):
        result = await dispatch_once(self.journal, "one", AsyncMock(return_value={"dealReference": "o_ref"}), self.reconcile)
        self.assertEqual(result["state"], "CONFIRMED")
        self.client.confirmation.assert_awaited_once_with("o_ref")
        self.assertEqual(self.client.session.await_count, 2)

    async def test_excess_slippage_or_size_never_confirmed(self):
        self.acknowledge()
        self.payload["level"] = "2000.11"
        self.position["level"] = "2000.11"
        self.assertIn("ACTUAL_FILL_EXCEEDS_APPROVED_ENVELOPE", (await self.reconcile())["reasons"])
        self.payload["size"] = "0.05"
        self.assertIn("FILL_SIZE_OR_DIRECTION_MISMATCH", (await self.reconcile())["reasons"])

    async def test_missing_or_loosened_broker_stop_never_confirmed(self):
        self.acknowledge()
        self.position["stopLevel"] = None
        self.assertEqual((await self.reconcile())["outcome"], "UNKNOWN")
        self.position["stopLevel"] = 1989
        self.assertIn("STOP_LOOSENED", (await self.reconcile())["reasons"])

    async def test_confirmation_failure_still_reads_all_broker_state(self):
        self.acknowledge()
        self.client.confirmation.side_effect = RuntimeError("private exception")
        result = await self.reconcile()
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.client.positions.assert_awaited_once()
        self.client.working_orders.assert_awaited_once()
        self.client.activity.assert_awaited_once()
        self.assertNotIn("private exception", str(result))

    async def test_no_reference_is_unknown_even_when_account_is_empty(self):
        self.journal.transition("one", "SUBMITTING")
        self.journal.transition("one", "UNKNOWN")
        self.client.positions.return_value = {"positions": []}
        self.assertEqual((await self.reconcile())["outcome"], "UNKNOWN")
        self.client.confirmation.assert_not_called()
        self.client.activity.assert_awaited_once()

    async def test_rejection_requires_empty_matching_account(self):
        self.acknowledge()
        self.payload.update(dealStatus="REJECTED", affectedDeals=[])
        self.assertEqual((await self.reconcile())["outcome"], "UNKNOWN")
        self.client.positions.return_value = {"positions": []}
        self.assertEqual((await self.reconcile())["outcome"], "REJECTED")
        self.client.session.return_value = {"accountId": "other", "currency": "USD"}
        self.assertEqual((await self.reconcile())["outcome"], "UNKNOWN")

    async def test_wrong_multiplier_detected_against_approved_risk(self):
        self.acknowledge()
        self.contract = replace(self.contract, value_per_point=D(2))
        self.assertIn("ACTUAL_FILL_EXCEEDS_APPROVED_ENVELOPE", (await self.reconcile())["reasons"])

    async def test_read_failure_is_not_missing_empty_position(self):
        self.acknowledge()
        self.client.positions.side_effect = TimeoutError("private details")
        result = await self.reconcile()
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertNotIn("private details", str(result))

    async def test_short_fill_slippage_direction_and_stop_risk(self):
        self.store.close()
        self.store = Store(Path(self.tmp.name)/"short.db")
        self.journal = OrderJournal(self.store)
        plan = replace(self.plan, signal=replace(self.plan.signal, direction=Direction.SHORT, stop=D(2010), target=D(1970)))
        self.journal.prepare(plan, "GOLD", sha256(b"account").hexdigest())
        self.acknowledge()
        self.payload.update(direction="SELL", level="1999.9")
        self.position.update(direction="SELL", level="1999.9", stopLevel=2010)
        self.assertEqual((await self.reconcile())["outcome"], "CONFIRMED")
        self.payload["level"] = "1999.89"
        self.position["level"] = "1999.89"
        self.assertEqual((await self.reconcile())["outcome"], "UNKNOWN")
