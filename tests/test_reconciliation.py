import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from gold_system.capital import CapitalDemo
from gold_system.core import D
from gold_system.reconciliation import ExpectedPosition, Snapshot, evaluate, inspect_demo, verify_open_confirmation
from gold_system.store import Store


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.snapshot = Snapshot({"accountId": "private-account", "currency": "USD"},
            {"accountId": "private-account", "currency": "USD"}, {"hedgingMode": False},
            {"positions": []}, {"workingOrders": []}, {"activities": []}, self.now, 1.2)
        self.expected = ExpectedPosition("deal-1", "BUY", D("0.02"), D(1990), D(2000))

    def position_snapshot(self, **overrides):
        position = {"dealId": "deal-1", "currency": "USD", "direction": "BUY",
                    "size": "0.02", "level": 2000, "stopLevel": 1990, **overrides}
        return replace(self.snapshot, positions={"positions": [
            {"position": position, "market": {"epic": "GOLD"}}]})

    def check(self, snapshot, expected=(), **kwargs):
        return evaluate(snapshot, expected, now=self.now, **kwargs)

    def test_flat_is_not_an_unlock_and_no_personal_identifier(self):
        result = self.check(self.snapshot)
        self.assertEqual(result["state"], "SNAPSHOT_MATCHED")
        self.assertFalse(result["entries_enabled"])
        self.assertNotIn("private-account", repr(result))
        self.assertNotIn("private-account", repr(self.snapshot))

    def test_unknown_intent_survives_empty_broker_account(self):
        result = self.check(self.snapshot, unresolved_intents=["unknown"])
        self.assertIn("UNRESOLVED_INTENT", result["reasons"])

    def test_account_switch_and_currency_and_hedging(self):
        s = replace(self.snapshot, session_after={"accountId": "other", "currency": "EUR"},
                    preferences={"hedgingMode": True})
        result = self.check(s)
        self.assertEqual(result["reasons"], ["ACCOUNT_CHANGED", "ACCOUNT_CURRENCY", "NETTING_MODE_UNPROVEN"])
        self.assertIn("ACCOUNT_MISMATCH", self.check(self.snapshot, expected_account_hash="wrong")["reasons"])

    def test_stale_future_and_slow_snapshot(self):
        for snapshot in [replace(self.snapshot, received_at=self.now-timedelta(seconds=3)),
                         replace(self.snapshot, received_at=self.now+timedelta(seconds=1)),
                         replace(self.snapshot, elapsed_seconds=2.1)]:
            self.assertIn("SNAPSHOT_STALE_OR_SLOW", self.check(snapshot)["reasons"])

    def test_position_requires_matching_local_state_and_stop(self):
        self.assertEqual(self.check(self.position_snapshot(), [self.expected])["state"], "SNAPSHOT_MATCHED")
        self.assertIn("UNTRACKED_POSITION", self.check(self.position_snapshot())["reasons"])
        self.assertIn("EXPECTED_POSITION_MISSING", self.check(self.snapshot, [self.expected])["reasons"])
        for stop in (None, "NaN", "Infinity", 0, True):
            self.assertIn("BROKER_SCHEMA_OR_STOP_UNPROVEN",
                          self.check(self.position_snapshot(stopLevel=stop), [self.expected])["reasons"])

    def test_partial_size_is_not_silently_accepted(self):
        self.assertIn("POSITION_MISMATCH", self.check(self.position_snapshot(size="0.01"), [self.expected])["reasons"])

    def test_stops_can_only_tighten_both_directions(self):
        self.assertIn("STOP_LOOSENED", self.check(self.position_snapshot(stopLevel=1980), [self.expected])["reasons"])
        self.assertEqual(self.check(self.position_snapshot(stopLevel=2001), [self.expected])["state"], "SNAPSHOT_MATCHED")
        short = replace(self.expected, direction="SELL", stop=D(2010))
        self.assertIn("STOP_LOOSENED", self.check(self.position_snapshot(direction="SELL", stopLevel=2020), [short])["reasons"])
        self.assertEqual(self.check(self.position_snapshot(direction="SELL", stopLevel=1999), [short])["state"], "SNAPSHOT_MATCHED")

    def test_unknown_working_order_does_not_get_cancelled(self):
        s = replace(self.snapshot, orders={"workingOrders": [{}]})
        self.assertIn("WORKING_ORDERS_REQUIRE_RECONCILIATION", self.check(s)["reasons"])

    def test_schema_missing_is_not_empty_account(self):
        for s in (replace(self.snapshot, positions={}), replace(self.snapshot, activity={}),
                  replace(self.snapshot, preferences={}), replace(self.snapshot, orders={"workingOrders": None})):
            self.assertEqual(self.check(s)["state"], "RECOVERY_LOCKED")

    def test_duplicate_and_foreign_positions_lock(self):
        s = self.position_snapshot()
        s.positions["positions"].append(copy.deepcopy(s.positions["positions"][0]))
        self.assertIn("MULTIPLE_POSITIONS", self.check(s)["reasons"])
        self.assertIn("BROKER_SCHEMA_OR_STOP_UNPROVEN", self.check(s)["reasons"])
        s = self.position_snapshot()
        s.positions["positions"][0]["market"]["epic"] = "SILVER"
        self.assertIn("UNEXPECTED_INSTRUMENT", self.check(s)["reasons"])

    def test_read_failure_persists_lock_without_exception_secret(self):
        client = Mock()
        client.session.side_effect = RuntimeError("secret credential")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"audit.db")
            try:
                store.claim("intent-1")
                report = inspect_demo(client, store)
                self.assertEqual(report["state"], "RECOVERY_LOCKED")
                self.assertNotIn("secret credential", repr(store.events()))
                self.assertEqual(store.unresolved_intents(), ["intent-1"])
                self.assertEqual(client.session.call_count, 1)
            finally:
                store.close()

    def test_rest_inspection_methods_are_get_only(self):
        client = CapitalDemo()
        client._request = Mock(return_value=({}, {}))
        client.session(); client.preferences(); client.positions(); client.working_orders()
        client.confirmation("id/?"); client.activity()
        for call in client._request.call_args_list:
            self.assertEqual(call.args[0], "GET")
        self.assertEqual(client._request.call_args_list[-2].args[1], "/confirms/id%2F%3F")

    def confirmation(self, **overrides):
        return {"dealReference": "order-1", "dealStatus": "ACCEPTED", "status": "OPEN",
                "affectedDeals": [{"dealId": "deal-1", "status": "OPENED"}],
                "epic": "GOLD", "direction": "BUY", "size": "0.02", "level": 2000, **overrides}

    def verify(self, payload, snapshot=None):
        return verify_open_confirmation("order-1", payload, self.expected,
                                        snapshot or self.position_snapshot(), now=self.now)

    def test_reference_alone_is_not_fill(self):
        self.assertEqual(self.verify({"dealReference": "order-1"})["outcome"], "UNKNOWN")
        self.assertEqual(self.verify(self.confirmation())["outcome"], "CONFIRMED")
        self.assertFalse(self.verify(self.confirmation())["entries_enabled"])

    def test_confirmation_without_broker_position_is_unknown(self):
        result = self.verify(self.confirmation(), self.snapshot)
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertIn("EXPECTED_POSITION_MISSING", result["reasons"])

    def test_confirm_partial_multiple_wrong_direction_and_reference(self):
        for changes in ({"size": "0.01"}, {"direction": "SELL"}, {"dealReference": "other"},
                        {"affectedDeals": []}, {"level": 2001}, {"status": "CLOSED"},
                        {"affectedDeals": [{"dealId": "deal-1", "status": "OPENED"}]*2}):
            self.assertEqual(self.verify(self.confirmation(**changes))["outcome"], "UNKNOWN")

    def test_rejected_requires_matching_reference_and_no_affected_deals(self):
        rejected = {"dealReference": "order-1", "dealStatus": "REJECTED"}
        self.assertEqual(self.verify(rejected)["outcome"], "REJECTED")
        self.assertEqual(self.verify({**rejected, "dealReference": "other"})["outcome"], "UNKNOWN")
        self.assertEqual(self.verify({**rejected, "affectedDeals": [{}]})["outcome"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
