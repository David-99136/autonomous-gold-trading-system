import asyncio
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from gold_system.core import Bar, D
from gold_system.analysis import zones
from gold_system.replay import aggregate, read_candles, replay_csv


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)

    def test_only_complete_aligned_bars(self):
        bars = [Bar(self.t + timedelta(minutes=i), D(10), D(12), D(9), D(11)) for i in range(1, 6)]
        self.assertEqual(len(aggregate(bars, 5)), 1)
        self.assertEqual(aggregate(bars[:-1], 5), [])
        self.assertEqual(aggregate(bars[1:], 5), [])

    def test_zone_formation_and_invalidation(self):
        base = Bar(self.t, D(10), D(12), D(9), D(11))
        impulse = Bar(self.t+timedelta(hours=1), D(11), D(15), D(10), D(14))
        self.assertEqual(zones([base]), [])
        self.assertEqual(zones([base, impulse])[0].kind, "DEMAND")
        invalid = Bar(self.t+timedelta(hours=2), D(14), D(14), D(7), D(8))
        self.assertFalse(any(z.kind == "DEMAND" for z in zones([base, impulse, invalid])))

    def make_csv(self, path, duplicate=False):
        fields = ["timestamp", *[f"{s}_{k}" for s in ("bid", "ask") for k in ("open", "high", "low", "close")],
                  "boundary", "tradeable", "liquid_window", "spread_limit", "extreme_volatility"]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fields)
            writer.writeheader()
            for i in range(1, 8):
                row = dict(timestamp=(self.t+timedelta(minutes=1 if duplicate else i)).isoformat(),
                           boundary=(self.t+timedelta(hours=8)).isoformat(), tradeable="true",
                           liquid_window="true", spread_limit="1", extreme_volatility="false")
                row.update({f"{s}_{k}": str(D(2000) + (D("0.5") if s == "ask" else 0))
                            for s in ("bid", "ask") for k in ("open", "high", "low", "close")})
                writer.writerow(row)

    def test_csv_end_to_end_insufficient_data_no_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            self.make_csv(p/"bars.csv")
            (p/"contract.json").write_text(json.dumps(dict(epic="TEST", value_per_point="1", minimum="0.01",
                increment="0.01", maximum="100", margin_rate="0.01", minimum_stop="0.1")))
            result = asyncio.run(replay_csv(p/"bars.csv", p/"contract.json", p/"run"))
            self.assertEqual(result["bars"], 7)
            self.assertEqual(result["metrics"]["trades"], 0)
            self.assertFalse(result["qualified"])
            self.assertTrue((p/"run/report.json").exists())

    def test_csv_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"bars.csv"
            self.make_csv(p, duplicate=True)
            with self.assertRaises(ValueError):
                read_candles(p)
