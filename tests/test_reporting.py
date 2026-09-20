import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from gold_system.reporting import render, summarize, write_report
from gold_system.store import Store


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "events.db"
        store = Store(self.db)
        store.close()
        self.start = "2026-09-20T00:00:00+08:00"
        self.end = "2026-09-21T00:00:00+08:00"

    def emit(self, kind, payload, recorded="2026-09-20T12:00:00+08:00"):
        db = sqlite3.connect(self.db)
        try:
            db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                       (recorded, kind, json.dumps(payload)))
            db.commit()
        finally:
            db.close()

    def summary(self, mode="PAPER"):
        return summarize(self.db, self.start, self.end, mode)

    def test_partial_exits_are_segments_not_wins_or_full_trades(self):
        for pnl in ("1.2", "-0.2"):
            self.emit("EXIT", {"simulated_at": self.start, "pnl": pnl})
        result = self.summary()
        self.assertEqual(result["paper_recorded_exit_pnl"], "1.0")
        self.assertEqual(result["paper_exit_segments"], 2)
        self.assertIsNone(result["completed_trades"])
        self.assertIsNone(result["realized_net_pnl"])
        self.assertIsNone(result["win_rate"])

    def test_boundary_uses_simulated_time_and_excludes_end(self):
        self.emit("ENTRY", {"simulated_at": "2026-09-19T16:00:00+00:00"}, recorded=self.end)
        self.emit("EXIT", {"simulated_at": self.end, "pnl": "99"})
        self.assertEqual(self.summary()["event_counts"], {"ENTRY": 1})

    def test_demo_does_not_consume_paper_fills_or_claim_flat(self):
        self.emit("EXIT", {"simulated_at": self.start, "pnl": "100"})
        self.emit("BROKER_RECONCILIATION", {"positions": []})
        result = self.summary("DEMO")
        self.assertEqual(result["event_counts"], {"BROKER_RECONCILIATION": 1})
        self.assertIsNone(result["paper_recorded_exit_pnl"])
        self.assertIsNone(result["remaining_positions"])

    def test_empty_database_does_not_mean_zero_pnl(self):
        result = self.summary()
        self.assertIsNone(result["paper_recorded_exit_pnl"])
        self.assertIsNone(result["costs"])
        self.assertIn("待核對", render(result))

    def test_missing_database_is_not_created(self):
        missing = self.db.parent / "missing.db"
        with self.assertRaises(FileNotFoundError):
            summarize(missing, self.start, self.end, "DEMO")
        self.assertFalse(missing.exists())

    def test_invalid_times_and_nonfinite_pnl_fail(self):
        for start, end in (("2026-09-20", self.end), (self.end, self.start)):
            with self.assertRaises(ValueError):
                summarize(self.db, start, end, "PAPER")
        self.emit("EXIT", {"simulated_at": self.start, "pnl": "NaN"})
        with self.assertRaises(ValueError):
            self.summary()

    def test_report_does_not_leak_payload_or_unknown_kind(self):
        self.emit("secret-canary", {"simulated_at": self.start, "identifier": "secret-canary"})
        self.assertNotIn("secret-canary", render(self.summary()))

    def test_existing_output_and_source_are_preserved(self):
        output = self.db.parent / "report.md"
        before = self.db.read_bytes()
        write_report(self.db, self.start, self.end, "DEMO", output)
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            write_report(self.db, self.start, self.end, "DEMO", output)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self.db.read_bytes(), before)

    def test_malformed_payload_fails_without_report(self):
        self.emit("EXIT", ["bad"])
        output = self.db.parent / "report.md"
        with self.assertRaises(ValueError):
            write_report(self.db, self.start, self.end, "PAPER", output)
        self.assertFalse(output.exists())

    def test_cli_writes_offline_report_without_broker_login(self):
        from gold_system.cli import main
        output = self.db.parent / "cli-report.md"
        args = ["gold-system", "daily-report", "--database", str(self.db),
                "--start", self.start, "--end", self.end, "--mode", "DEMO",
                "--output", str(output)]
        captured = io.StringIO()
        with patch("sys.argv", args), redirect_stdout(captured), \
                patch("gold_system.capital.load_credentials") as credentials:
            main()
        credentials.assert_not_called()
        self.assertTrue(output.is_file())
        self.assertFalse(json.loads(captured.getvalue())["broker_access"])
