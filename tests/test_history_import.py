"""合成快照驗證離線入口；不得碰觸使用者帳戶或真實交易資料庫。

[Author: Antigravity | Date: 2026-09-27]
"""
import contextlib
import copy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gold_system.cli import main
from gold_system.history_import import MAX_INPUT_BYTES, audit_file
from gold_system.store import Store


class HistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "audit.db"
        self.source = self.root / "source.json"
        self.args = dict(start="2026-01-05T10:00:00Z", end="2026-01-05T10:02:00Z",
                         received_at="2026-01-05T10:04:00Z")
        self.response = {"prices": [dict(snapshotTimeUTC=f"2026-01-05T10:0{i}:00",
            openPrice=dict(bid=100, ask=101), highPrice=dict(bid=102, ask=103),
            lowPrice=dict(bid=99, ask=100), closePrice=dict(bid=101, ask=102)) for i in range(3)]}
        self.write(self.response)
        Store(self.database).close()

    def write(self, response):
        self.source.write_text(json.dumps(response), encoding="utf-8")

    def audit(self, **changes):
        return audit_file(self.database, self.source, **(self.args | changes))

    def locked(self):
        store = Store(self.database, must_exist=True)
        try:
            return store.entries_stopped()
        finally:
            store.close()

    def test_normal_observation_has_auditable_link_but_no_permission(self):
        result, code = self.audit()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "OBSERVED_UNVERIFIED")
        self.assertEqual(result["database_purpose"], "existing_unverified")
        self.assertEqual(result["received_at_provenance"], "CALLER_SUPPLIED")
        self.assertFalse(result["source_verified"])
        self.assertFalse(result["entries_enabled"])
        self.assertFalse(result["closure_verified"])
        self.assertLessEqual(result["imported_at"], result["verified_at"])
        self.assertLessEqual(result["verified_at"], result["completed_at"])
        store = Store(self.database)
        try:
            row = store.db.execute("SELECT payload FROM history_quality_observations WHERE id=?",
                                   (result["observation_id"],)).fetchone()
            self.assertEqual(json.loads(row[0])["price_fingerprint_hash"], result["price_fingerprint_hash"])
            self.assertEqual(store.events()[-1]["payload"], result)
        finally:
            store.close()

    def test_duplicate_import_retains_first_observation(self):
        self.audit()
        result, code = self.audit(received_at="2026-01-05T10:05:00Z")
        self.assertEqual(code, 0)
        self.assertEqual(result["revised_rows"], 0)
        store = Store(self.database)
        try:
            self.assertEqual(store.db.execute("SELECT DISTINCT received_at FROM history_price_first").fetchall(),
                             [("2026-01-05T10:04:00+00:00",)])
        finally:
            store.close()

    def test_cross_revision_restart_and_good_values_cannot_unlock(self):
        broken = copy.deepcopy(self.response)
        broken["prices"][0]["closePrice"]["ask"] = 100
        self.write(broken)
        self.assertIn("HISTORY_BID_ASK_CROSSED", self.audit()[0]["reasons"])
        self.write(self.response)
        for _ in range(2):
            result, code = self.audit()
            self.assertEqual(code, 2)
            self.assertIn("HISTORY_PRICE_REVISION", result["reasons"])
            self.assertTrue(self.locked())

    def test_bad_prices_and_windows_fail_closed(self):
        cases = []
        bad = copy.deepcopy(self.response)
        bad["prices"][0]["highPrice"]["bid"] = 98
        cases.append((bad, "HISTORY_OHLC_INVALID"))
        cases.append(({"prices": self.response["prices"][::2]}, "HISTORY_GAP"))
        cases.append(({"prices": self.response["prices"][::-1]}, "HISTORY_ORDER_INVALID"))
        cases.append(({"prices": self.response["prices"] + [self.response["prices"][-1]]}, "HISTORY_ORDER_INVALID"))
        for payload, reason in cases:
            with self.subTest(reason=reason):
                self.write(payload)
                result, code = self.audit()
                self.assertEqual(code, 2)
                self.assertIn(reason, result["reasons"])
                self.assertTrue(self.locked())

    def test_missing_source_locks_existing_store(self):
        self.source.unlink()
        result, code = self.audit()
        self.assertEqual(code, 1)
        self.assertEqual(result["reasons"], ["HISTORY_INPUT_READ_FAILED"])
        self.assertTrue(result["lock_persisted"])
        self.assertTrue(self.locked())

    def test_malformed_json_and_secrets_never_escape(self):
        for raw in ('{"password":"secret-canary",', '{"prices":[],"prices":[]}',
                    '{"prices":NaN}', '[' * 2000, '\ud800'):
            with self.subTest(raw_length=len(raw)):
                self.source.write_bytes(raw.encode("utf-8", errors="surrogatepass"))
                result, code = self.audit()
                self.assertEqual(code, 1)
                self.assertEqual(result["reasons"], ["HISTORY_JSON_INVALID"])
                self.assertTrue(self.locked())
                self.assertNotIn("secret-canary", json.dumps(result))

    def test_valid_extra_secret_fields_are_not_journaled(self):
        self.response["arbitrary"] = {"password": "secret-canary"}
        self.response["prices"][0]["unknown"] = "secret-canary"
        self.write(self.response)
        result, code = self.audit()
        self.assertEqual(code, 0)
        store = Store(self.database)
        try:
            self.assertNotIn("secret-canary", json.dumps(store.events()))
            self.assertNotIn(str(self.source), json.dumps(result))
        finally:
            store.close()

    def test_file_size_limit(self):
        self.source.write_bytes(b" " * (MAX_INPUT_BYTES + 1))
        result, code = self.audit()
        self.assertEqual(code, 1)
        self.assertEqual(result["reasons"], ["HISTORY_INPUT_TOO_LARGE"])
        self.assertTrue(self.locked())

    def test_directory_input_rejected(self):
        self.source = self.root
        self.assertEqual(self.audit()[1], 1)
        self.assertTrue(self.locked())

    def test_unc_and_url_paths_rejected_before_open(self):
        for source in ("//host/share/data.json", "https://example.invalid/data.json"):
            with self.subTest(source=source):
                result, code = audit_file(self.database, source, **self.args)
                self.assertEqual(code, 1)
                self.assertEqual(result["reasons"], ["HISTORY_INPUT_READ_FAILED"])
                self.assertTrue(self.locked())

    def test_invalid_naive_and_future_times_lock_without_echo(self):
        for value in ("secret-canary", "2026-01-05T10:00:00", "9999-01-01T00:00:00Z"):
            result, code = self.audit(received_at=value)
            self.assertEqual(code, 1)
            self.assertNotIn("secret-canary", json.dumps(result))
            self.assertTrue(self.locked())

    def test_window_limit_and_unaligned_end(self):
        for changes in (dict(start="2026-01-01T00:00:00Z"), dict(end="2026-01-05T10:02:01Z")):
            result, code = self.audit(**changes)
            self.assertEqual(code, 2)
            self.assertIn("HISTORY_REQUEST_INVALID", result["reasons"])

    def test_missing_database_not_created(self):
        self.database = self.root / "typo.db"
        result, code = self.audit()
        self.assertEqual(code, 1)
        self.assertIsNone(result["lock_persisted"])
        self.assertFalse(self.database.exists())

    def test_non_store_sqlite_is_not_initialized(self):
        self.database = self.root / "unrelated.db"
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE unrelated (x)")
        self.assertEqual(self.audit()[1], 1)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])

    def test_new_diagnostic_is_explicit_and_persistent(self):
        self.database = self.root / "diagnostic.db"
        result, code = self.audit(create_diagnostic=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["database_purpose"], "diagnostic")
        self.assertEqual(self.audit()[0]["database_purpose"], "diagnostic")

    def test_create_diagnostic_never_overwrites_existing(self):
        before = self.database.read_bytes()
        result, code = self.audit(create_diagnostic=True)
        self.assertEqual(code, 1)
        self.assertIsNone(result["lock_persisted"])
        self.assertEqual(before, self.database.read_bytes())

    def test_other_locks_preserved(self):
        store = Store(self.database)
        store.set("daily_new_entries_blocked", True)
        store.stop_new_entries()
        store.close()
        self.assertEqual(self.audit()[1], 0)
        self.assertTrue(self.locked())

    def test_monitor_failure_attempts_persistent_lock_without_raw_error(self):
        with patch("gold_system.history_import.HistoryQualityMonitor.observe", side_effect=RuntimeError("secret-canary")):
            result, code = self.audit()
        self.assertEqual(code, 1)
        self.assertTrue(result["lock_persisted"])
        self.assertTrue(self.locked())
        self.assertNotIn("secret-canary", json.dumps(result))

    def test_finish_journal_failure_also_locks(self):
        original = Store.emit
        def emit(store, kind, **payload):
            if kind == "HISTORY_IMPORT_FINISHED":
                raise sqlite3.OperationalError("secret-canary")
            return original(store, kind, **payload)
        with patch.object(Store, "emit", emit):
            result, code = self.audit()
        self.assertEqual(code, 1)
        self.assertTrue(result["lock_persisted"])
        self.assertTrue(self.locked())

    def test_database_failure_does_not_claim_successful_lock(self):
        store = Store(self.database)
        store.db.execute("CREATE TRIGGER fail_state BEFORE INSERT ON state BEGIN SELECT RAISE(ABORT, 'secret-canary'); END")
        store.db.commit()
        store.close()
        self.source.unlink()
        result, code = self.audit()
        self.assertEqual(code, 1)
        self.assertIsNone(result["lock_persisted"])
        self.assertIn("HISTORY_LOCK_WRITE_FAILED", result["reasons"])
        self.assertNotIn("secret-canary", json.dumps(result))

    def argv(self):
        return ["gold_system", "history-check", "--input", str(self.source), "--database", str(self.database),
                "--start", self.args["start"], "--end", self.args["end"], "--received-at", self.args["received_at"]]

    def test_cli_does_not_connect_network_or_read_credentials(self):
        out = io.StringIO()
        with patch.object(sys, "argv", self.argv()), contextlib.redirect_stdout(out), \
                patch("socket.socket.connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), \
                patch("gold_system.capital.load_credentials", side_effect=AssertionError("CREDENTIALS_FORBIDDEN")):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "OBSERVED_UNVERIFIED")

    def test_real_cli_error_exit_is_sanitized(self):
        self.source.write_text("secret-canary", encoding="utf-8")
        completed = subprocess.run([sys.executable, "-m", "gold_system"] + self.argv()[1:],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("secret-canary", completed.stdout + completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["lock_persisted"])

    def test_no_unlock_or_closure_flags(self):
        for flag in ("--unlock", "--closure-verified", "--live"):
            with patch.object(sys, "argv", self.argv() + [flag]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 2)
