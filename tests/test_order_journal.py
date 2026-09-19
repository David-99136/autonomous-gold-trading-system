import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from gold_system.core import D, Direction, Mode, Signal, Plan, Rejected
from gold_system.order_journal import OrderJournal, dispatch_once
from gold_system.store import Store


class OrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"journal.db"
        self.store = Store(self.path)
        self.journal = OrderJournal(self.store)
        now = datetime.now(timezone.utc)
        signal = Signal("order1", "test", now, now+timedelta(minutes=1), Direction.LONG,
                        Mode.RIGHT, D(1990), D(2030), "Not stored: private prose")
        self.plan = Plan(signal, D(2000), D("0.1"), D("1.02"), D(10))
        self.journal.prepare(self.plan, "GOLD", "a"*64)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_flow(self, send, reconcile):
        return asyncio.run(dispatch_once(self.journal, "order1", send, reconcile))

    def test_success_requires_reconciliation_not_reference_alone(self):
        send = AsyncMock(return_value={"dealReference": "o_ref"})
        reconcile = AsyncMock(return_value={"outcome": "CONFIRMED"})
        result = self.run_flow(send, reconcile)
        self.assertEqual(result["state"], "CONFIRMED")
        self.assertFalse(result["entries_enabled"])
        reconcile.assert_awaited_once_with("o_ref")
        self.assertEqual([e["payload"]["state"] for e in self.store.events()],
                         ["PREPARED", "SUBMITTING", "ACKNOWLEDGED", "CONFIRMED"])
        with self.assertRaises(Rejected):
            self.run_flow(send, reconcile)
        send.assert_awaited_once()

    def test_send_happens_only_after_committed_submitting_state(self):
        async def send():
            second_store = Store(self.path)
            try:
                self.assertEqual(OrderJournal(second_store).get("order1")["state"], "SUBMITTING")
            finally:
                second_store.close()
            return {"dealReference": "o_ref"}
        self.run_flow(send, AsyncMock(return_value={"outcome": "UNKNOWN"}))

    def test_timeout_without_reference_reads_state_but_never_retries(self):
        send = AsyncMock(side_effect=TimeoutError("private broker text"))
        reconcile = AsyncMock(return_value={"outcome": "REJECTED"})
        result = self.run_flow(send, reconcile)
        self.assertEqual(result["state"], "UNKNOWN")
        reconcile.assert_awaited_once_with(None)
        self.assertEqual(self.journal.unresolved(), ["order1"])
        with self.assertRaises(Rejected):
            self.run_flow(send, reconcile)
        send.assert_awaited_once()
        self.assertNotIn("private broker text", str(self.store.events()))

    def test_acknowledgement_survives_failed_confirmation(self):
        self.run_flow(AsyncMock(return_value={"dealReference": "o_ref"}), AsyncMock(side_effect=TimeoutError()))
        self.assertEqual(self.journal.get("order1")["reference"], "o_ref")
        self.assertEqual(self.journal.get("order1")["state"], "UNKNOWN")

    def test_restart_retains_no_resubmission(self):
        self.journal.transition("order1", "SUBMITTING")
        self.store.close()
        self.store = Store(self.path)
        self.journal = OrderJournal(self.store)
        send = AsyncMock()
        with self.assertRaises(Rejected):
            self.run_flow(send, AsyncMock())
        send.assert_not_called()
        self.assertEqual(self.journal.unresolved(), ["order1"])

    def test_idempotent_prepare_conflict_and_secret_prose_excluded(self):
        self.assertFalse(self.journal.prepare(self.plan, "GOLD", "a"*64))
        with self.assertRaises(Rejected):
            self.journal.prepare(replace(self.plan, size=D(1)), "GOLD", "a"*64)
        with self.assertRaises(Rejected):
            self.journal.prepare(self.plan, "GOLD", "b"*64)
        self.assertNotIn("private prose", str(self.journal.get("order1")))

    def test_same_account_second_open_is_blocked_even_after_confirmation(self):
        self.run_flow(AsyncMock(return_value={"dealReference": "o_ref"}), AsyncMock(return_value={"outcome": "CONFIRMED"}))
        second = replace(self.plan, signal=replace(self.plan.signal, signal_id="order2"))
        self.journal.prepare(second, "GOLD", "a"*64)
        with self.assertRaises(Rejected):
            self.journal.transition("order2", "SUBMITTING")

    def test_expired_prepared_order_cannot_dispatch(self):
        expired = replace(self.plan, signal=replace(self.plan.signal, signal_id="old",
                          expires=datetime.now(timezone.utc)-timedelta(seconds=1)))
        self.journal.prepare(expired, "GOLD", "a"*64)
        with self.assertRaises(Rejected):
            self.journal.transition("old", "SUBMITTING")

    def test_rejected_confirmation_is_terminal(self):
        result = self.run_flow(AsyncMock(return_value={"dealReference": "o_ref"}), AsyncMock(return_value={"outcome": "REJECTED"}))
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(self.journal.unresolved(), [])

    def test_cancellation_leaves_unknown(self):
        with self.assertRaises(asyncio.CancelledError):
            self.run_flow(AsyncMock(side_effect=asyncio.CancelledError()), AsyncMock())
        self.assertEqual(self.journal.get("order1")["state"], "UNKNOWN")

    def test_invalid_reference_is_unknown_and_not_recorded(self):
        result = self.run_flow(AsyncMock(return_value={"dealReference": "https://secret"}),
                               AsyncMock(return_value={"outcome": "CONFIRMED"}))
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertNotIn("secret", str(self.journal.get("order1")))

    def test_reference_cannot_be_overwritten(self):
        self.journal.transition("order1", "SUBMITTING")
        self.journal.transition("order1", "ACKNOWLEDGED", reference="o_ref")
        self.journal.transition("order1", "UNKNOWN")
        with self.assertRaises(Rejected):
            self.journal.transition("order1", "ACKNOWLEDGED", reference="other")
        self.assertEqual(self.journal.get("order1")["reference"], "o_ref")

    def test_deadline_cancels_wait_and_still_reconciles(self):
        async def never_responds():
            await asyncio.Event().wait()
        reconcile = AsyncMock(return_value={"outcome": "UNKNOWN"})
        result = asyncio.run(dispatch_once(self.journal, "order1", never_responds, reconcile, timeout_seconds=.01))
        self.assertEqual(result["state"], "UNKNOWN")
        reconcile.assert_awaited_once_with(None)

    def test_concurrent_dispatch_of_same_intent_calls_send_once(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def send():
                entered.set()
                await release.wait()
                return {"dealReference": "o_ref"}
            send_mock = AsyncMock(side_effect=send)
            reconcile = AsyncMock(return_value={"outcome": "CONFIRMED"})
            first = asyncio.create_task(dispatch_once(self.journal, "order1", send_mock, reconcile))
            await entered.wait()
            try:
                with self.assertRaises(Rejected):
                    await dispatch_once(self.journal, "order1", send_mock, reconcile)
            finally:
                release.set()
                await first
            send_mock.assert_awaited_once()
        asyncio.run(scenario())
