import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gold_system.core import Bar, D, Direction, Mode, RiskPolicy, Signal
from gold_system.replay import Candle, replay_csv
from gold_system.stress import stress_paths


class StressTests(unittest.TestCase):
    def test_intratrade_loss_not_hidden_by_profitable_close(self):
        report = stress_paths([[D(0), D("-0.2"), D("0.01")]], simulations=10, block_size=1)
        self.assertEqual(report["maximum_drawdown_p95"], D("0.2"))
        self.assertFalse(report["qualified"])

    def test_full_block_preserves_consecutive_loss_compounding(self):
        paths = [[D(0), D("-0.1")], [D(0), D("-0.1")]]
        report = stress_paths(paths, simulations=10, block_size=2)
        self.assertEqual(report["maximum_drawdown_p95"], D("0.19"))

    def test_reproducible_seed_and_source_hash(self):
        paths = [[D(0), D("-0.01")], [D(0), D("0.02")], [D(0), D("-0.02")]]
        first = stress_paths(paths, simulations=100, block_size=1, seed=7)
        self.assertEqual(first, stress_paths(paths, simulations=100, block_size=1, seed=7))
        changed = stress_paths(paths[:-1], simulations=100, block_size=1, seed=7)
        self.assertNotEqual(first["path_sha256"], changed["path_sha256"])

    def test_extra_cost_applied_once_per_trade_not_per_mark(self):
        report = stress_paths([[D(0), D(0), D(0), D(0)]], simulations=10, block_size=1, extra_cost_fraction=D("0.02"))
        self.assertEqual(report["maximum_drawdown_p95"], D("0.02"))

    def test_ruin_does_not_recover_with_later_lucky_marks(self):
        report = stress_paths([[D(0), D("-1.1"), D(2)]], simulations=10, block_size=1)
        self.assertEqual(report["ruin_count"], 10)
        self.assertEqual(report["maximum_drawdown_p95"], D("1.1"))

    def test_invalid_missing_or_nonfinite_data_rejected(self):
        for paths in ([], [[D(0)]], [[D(1), D(2)]], [[D(0), D("NaN")]], [[0, .1]]):
            with self.assertRaises(ValueError):
                stress_paths(paths, block_size=1)
        with self.assertRaises(ValueError):
            stress_paths([[D(0), D(1)]], block_size=2)

    def test_replay_emits_complete_trade_path_with_final_close(self):
        now = datetime(2026, 9, 7, 0, tzinfo=timezone.utc)
        bars = []
        for i in range(1, 182):
            ts = now+timedelta(minutes=i)
            bid = Bar(ts, D(2000), D(2005), D(1999), D(2000))
            ask = Bar(ts, D("2000.5"), D("2005.5"), D("1999.5"), D("2000.5"))
            bars.append(Candle(bid, ask, True, True, now+timedelta(hours=8), D(1), False))
        def analysis(h1, m5, m1, at):
            return Signal(str(at.timestamp()), RiskPolicy().version, at, at+timedelta(minutes=1),
                          Direction.LONG, Mode.RIGHT, D(1990), D(2030), "SYNTHETIC_FIXTURE")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"input.csv").write_text("synthetic placeholder for source hash", encoding="utf-8")
            (root/"contract.json").write_text(json.dumps(dict(epic="TEST", value_per_point="1", minimum="0.01",
                 increment="0.01", maximum="100", margin_rate="0.01", minimum_stop="0.1")), encoding="utf-8")
            with patch("gold_system.replay.read_candles", return_value=bars), patch("gold_system.replay.analyze", side_effect=analysis):
                report = asyncio.run(replay_csv(root/"input.csv", root/"contract.json", root/"out"))
            self.assertEqual(report["metrics"]["trades"], 1)
            path = report["trade_paths"][0]
            self.assertEqual(path["returns"][0], 0)
            self.assertGreater(len(path["returns"]), 3)
            self.assertEqual(path["returns"][-1]*path["start_equity"], path["net_pnl"])
            self.assertEqual(path["net_pnl"], report["metrics"]["net_pnl"])
            self.assertLess(min(path["returns"]), path["returns"][-1])
            self.assertFalse(stress_paths([path["returns"]], simulations=10, block_size=1)["qualified"])
