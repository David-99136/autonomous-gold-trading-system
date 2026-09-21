import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import localcontext
from pathlib import Path

from gold_system.history_quality import HistoryQualityMonitor
from gold_system.store import Store


class HistoryQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audit.db"
        self.store = Store(self.path)
        self.monitor = HistoryQualityMonitor(self.store)
        self.start = datetime(2026, 9, 21, 2, 15, tzinfo=timezone.utc)
        self.args = dict(start=self.start, end=self.start + timedelta(minutes=2),
                         received_at=self.start + timedelta(minutes=4))
        self.response = {"prices": [self.row(i) for i in range(3)]}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def row(self, minute):
        return dict(snapshotTimeUTC=(self.start + timedelta(minutes=minute)).replace(tzinfo=None).isoformat(),
                    openPrice=dict(bid=100, ask=101), highPrice=dict(bid=102, ask=103),
                    lowPrice=dict(bid=99, ask=100), closePrice=dict(bid=101, ask=102))

    def observe(self, response=None, **changes):
        return self.monitor.observe(self.response if response is None else response, **(self.args | changes))

    def test_normal_data_never_grants_closure_or_entry_permission(self):
        result = self.observe()
        self.assertEqual(result["status"], "OBSERVED_UNVERIFIED")
        self.assertEqual(result["reasons"], [])
        self.assertFalse(result["closure_verified"])
        self.assertFalse(result["entries_enabled"])
        self.assertFalse(self.store.entries_stopped())

    def test_numeric_equivalence_and_volume_changes_are_not_price_revisions(self):
        self.observe()
        changed = copy.deepcopy(self.response)
        changed["prices"][0]["openPrice"]["bid"] = "100.00"
        changed["prices"][0]["lastTradedVolume"] = 999
        result = self.observe(changed)
        self.assertEqual(result["revised_rows"], 0)
        self.assertEqual(result["reasons"], [])

    def test_decimal_context_cannot_hide_a_revision(self):
        with localcontext() as context:
            context.prec = 2
            self.observe()
            changed = copy.deepcopy(self.response)
            changed["prices"][0]["highPrice"]["ask"] = "103.0001"
            self.assertIn("HISTORY_PRICE_REVISION", self.observe(changed)["reasons"])

    def test_crossed_snapshot_then_good_snapshot_stays_locked_and_detects_revision(self):
        broken = copy.deepcopy(self.response)
        broken["prices"][0]["closePrice"]["ask"] = 100
        result = self.observe(broken)
        self.assertIn("HISTORY_BID_ASK_CROSSED", result["reasons"])
        result = self.observe()
        self.assertIn("HISTORY_PRICE_REVISION", result["reasons"])
        self.assertTrue(self.store.entries_stopped())
        self.assertEqual(result["revised_rows"], 1)

    def test_each_price_field_revision_is_detected(self):
        self.observe()
        for side in ("bid", "ask"):
            for field in ("openPrice", "highPrice", "lowPrice", "closePrice"):
                with self.subTest(side=side, field=field):
                    changed = copy.deepcopy(self.response)
                    changed["prices"][0][field][side] += 0.1
                    self.assertIn("HISTORY_PRICE_REVISION", self.observe(changed)["reasons"])

    def test_restart_and_later_valid_snapshot_do_not_clear_lock(self):
        self.observe({"prices": []})
        self.store.close()
        self.store = Store(self.path)
        self.monitor = HistoryQualityMonitor(self.store)
        result = self.observe()
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertIn("HISTORY_RECOVERY_LOCKED", result["reasons"])
        self.assertTrue(self.store.entries_stopped())

    def test_restart_retains_first_price_fingerprint(self):
        self.observe()
        self.store.close()
        self.store = Store(self.path)
        self.monitor = HistoryQualityMonitor(self.store)
        changed = copy.deepcopy(self.response)
        changed["prices"][0]["highPrice"]["ask"] = 104
        self.assertIn("HISTORY_PRICE_REVISION", self.observe(changed)["reasons"])

    def test_invalid_numeric_values_are_fail_closed(self):
        for value in (None, True, "NaN", "Infinity", "0", "-1", "1e100", "1" * 81, {}):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.response)
                changed["prices"][0]["openPrice"]["bid"] = value
                self.assertIn("HISTORY_ROW_INVALID", self.observe(changed)["reasons"])

    def test_invalid_ohlc_is_not_accepted(self):
        changed = copy.deepcopy(self.response)
        changed["prices"][0]["highPrice"]["bid"] = 98
        self.assertIn("HISTORY_OHLC_INVALID", self.observe(changed)["reasons"])

    def test_missing_side_field_is_rejected(self):
        changed = copy.deepcopy(self.response)
        del changed["prices"][0]["closePrice"]["ask"]
        self.assertIn("HISTORY_ROW_INVALID", self.observe(changed)["reasons"])

    def test_duplicate_and_reversed_rows_are_rejected(self):
        for rows in ([self.row(0), self.row(0), self.row(2)], list(reversed(self.response["prices"]))):
            self.assertIn("HISTORY_ORDER_INVALID", self.observe({"prices": rows})["reasons"])

    def test_gaps_and_window_edges_are_not_filled(self):
        self.assertIn("HISTORY_GAP", self.observe({"prices": [self.row(0), self.row(2)]})["reasons"])
        self.assertIn("HISTORY_COVERAGE_INCOMPLETE", self.observe({"prices": [self.row(1), self.row(2)]})["reasons"])

    def test_out_of_range_and_nonminute_timestamps_are_rejected(self):
        changed = copy.deepcopy(self.response)
        changed["prices"][0] = self.row(-1)
        self.assertIn("HISTORY_RANGE_INVALID", self.observe(changed)["reasons"])
        changed["prices"][0]["snapshotTimeUTC"] = "2026-09-21T02:15:01Z"
        self.assertIn("HISTORY_ROW_INVALID", self.observe(changed)["reasons"])

    def test_bad_scope_times_and_oversized_window_block(self):
        for changes in (dict(epic="ETHUSD"), dict(resolution="HOUR"),
                        dict(start=self.start.replace(tzinfo=None)), dict(end=self.start-timedelta(minutes=1)),
                        dict(received_at=self.start), dict(end=self.start+timedelta(days=1),
                        received_at=self.start+timedelta(days=2))):
            self.assertIn("HISTORY_REQUEST_INVALID", self.observe(**changes)["reasons"])

    def test_empty_malformed_and_oversized_response_block(self):
        for response in ({}, [], {"prices": []}, {"prices": "secret-canary"}, {"prices": [self.row(0)] * 1001}):
            self.assertIn("HISTORY_ROWS_INVALID", self.observe(response)["reasons"])

    def test_backdating_does_not_change_first_received_time(self):
        self.observe()
        result = self.observe(received_at=self.args["received_at"]-timedelta(seconds=1))
        self.assertIn("HISTORY_RECEIPT_BACKDATED", result["reasons"])
        times = self.store.db.execute("SELECT DISTINCT received_at FROM history_price_first").fetchall()
        self.assertEqual(times, [(self.args["received_at"].isoformat(),)])

    def test_arbitrary_secret_fields_and_bad_values_never_enter_journal(self):
        changed = copy.deepcopy(self.response)
        changed["password"] = "secret-canary"
        changed["prices"][0]["closePrice"]["bid"] = "secret-canary"
        result = self.observe(changed)
        saved = repr(self.store.db.execute("SELECT * FROM history_quality_observations").fetchall())
        self.assertNotIn("secret-canary", saved + json.dumps(result) + repr(self.store.events()))

    def test_journal_and_lock_roll_back_together_on_storage_failure(self):
        self.store.db.execute("CREATE TRIGGER fail_observation BEFORE INSERT ON history_quality_observations BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(Exception):
            self.observe({"prices": [self.row(0)]})
        self.assertIsNone(self.store.get("market_data_blocked"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM history_price_first").fetchone()[0], 0)
        self.assertEqual(self.store.events(), [])

    def test_operator_and_daily_locks_are_not_removed(self):
        for key in ("operator_stop_new", "daily_new_entries_blocked"):
            self.store.set(key, True)
        self.observe()
        self.assertTrue(self.store.entries_stopped())
        self.assertTrue(self.store.get("operator_stop_new"))
        self.assertTrue(self.store.get("daily_new_entries_blocked"))

    def test_malformed_data_lock_is_fail_closed(self):
        self.store.set("market_data_blocked", "not-a-boolean")
        self.assertTrue(self.store.entries_stopped())
        self.assertEqual(self.observe()["status"], "RECOVERY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
