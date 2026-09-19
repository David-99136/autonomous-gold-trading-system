import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gold_system.core import Bar, Contract, D, Mode, Direction, RiskPolicy
from gold_system.replay import Candle
from gold_system.walk_forward import run_walk_forward, validate_result, windows, replay_evaluator


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.candles = []
        for i in range(1, 12):
            ts = self.start+timedelta(minutes=i)
            b = Bar(ts, D(2000), D(2005), D(1995), D(2001))
            self.candles.append(Candle(b, replace(b, open=D("2000.5"), high=D("2005.5"), low=D("1995.5"), close=D("2001.5")),
                                       True, True, self.start+timedelta(hours=8), D(1), False))
        self.config = {"strategy_version": "synthetic-test", "model_version": "NOT_USED", "prompt_version": "NOT_USED"}

    def evaluator(self, config, warmup, test):
        marks = [c.bid.timestamp-timedelta(seconds=offset) for c in test for offset in (60, 45, 30, 15)]
        marks.append(test[-1].bid.timestamp)
        return {"trades": [], "equity_points": [(t, D(10000)) for t in marks]}

    def test_contiguous_test_windows_with_purged_training(self):
        self.assertEqual(windows(11, 3, 2, 1), [(0, 3, 4, 6), (2, 5, 6, 8), (4, 7, 8, 10)])
        self.assertEqual(windows(4, 3, 2), [])

    def test_fit_never_receives_current_or_future_test_and_prepare_precedes_evaluate(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"walk"
            def fit(train):
                seen.append(train[-1].bid.timestamp)
                return dict(self.config)
            def evaluate(config, warmup, test):
                self.assertLess(seen[-1], test[0].bid.timestamp-timedelta(minutes=1))
                self.assertLess(warmup[-1].bid.timestamp, test[0].bid.timestamp)
                prepared = out/f"fold-{len(seen)-1:04d}-prepared.json"
                self.assertTrue(prepared.exists())
                self.assertFalse((out/f"fold-{len(seen)-1:04d}-result.json").exists())
                return self.evaluator(config, warmup, test)
            report = run_walk_forward(self.candles, fit, evaluate, out, train_size=3, test_size=2, purge_size=1)
            self.assertEqual(len(report["folds"]), 3)
            self.assertEqual(report["unused_tail_samples"], 1)
            self.assertFalse(report["qualified"])
            self.assertTrue((out/"summary.json").exists())

    def test_config_mutation_leaves_no_successful_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"walk"
            def bad(config, warmup, test):
                config["strategy_version"] = "retuned-after-test"
                return self.evaluator(config, warmup, test)
            with self.assertRaisesRegex(ValueError, "modified"):
                run_walk_forward(self.candles, lambda _: dict(self.config), bad, out, train_size=3, test_size=2)
            self.assertTrue((out/"fold-0000-prepared.json").exists())
            self.assertFalse((out/"fold-0000-result.json").exists())
            self.assertFalse((out/"summary.json").exists())

    def test_missing_intrabar_marks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            def sparse(config, warmup, test):
                start, end = test[0].bid.timestamp-timedelta(minutes=1), test[-1].bid.timestamp
                return {"trades": [], "equity_points": [(start, D(10000)), (end, D(10000))]}
            with self.assertRaisesRegex(ValueError, "intrabar"):
                run_walk_forward(self.candles, lambda _: dict(self.config), sparse, Path(tmp)/"walk", train_size=3, test_size=2)

    def test_trade_outside_window_and_accounting_mismatch_rejected(self):
        start, end = self.start, self.start+timedelta(minutes=2)
        trade = {"opened_at": start, "closed_at": end, "strategy_mode": Mode.RIGHT,
                 "direction": Direction.LONG, "net_pnl": D(1)}
        result = {"trades": [trade], "equity_points": [(start, D(100)), (end, D(101))]}
        self.assertEqual(validate_result(result, start, end)["trades"], 1)
        result["trades"] = [{**trade, "opened_at": start-timedelta(seconds=1)}]
        with self.assertRaises(ValueError):
            validate_result(result, start, end)
        result["trades"] = [{**trade, "net_pnl": D(2)}]
        with self.assertRaises(ValueError):
            validate_result(result, start, end)

    def test_repeated_run_hashes_match_and_existing_results_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = [run_walk_forward(self.candles, lambda _: dict(self.config), self.evaluator,
                         root/name, train_size=3, test_size=2) for name in ("first", "second")]
            self.assertEqual(reports[0], reports[1])
            with self.assertRaises(FileExistsError):
                run_walk_forward(self.candles, lambda _: dict(self.config), self.evaluator,
                                 root/"first", train_size=3, test_size=2)

    def test_duplicate_source_and_insufficient_history_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data in (self.candles[:3], [self.candles[0]]*10):
                with self.assertRaises(ValueError):
                    run_walk_forward(data, lambda _: dict(self.config), self.evaluator,
                                     Path(tmp)/"walk", train_size=3, test_size=2)

    def capital_evaluator(self, config, warmup, test, *, initial_equity):
        result = self.evaluator(config, warmup, test)
        marks = [t for t, _ in result["equity_points"]]
        profit = initial_equity * D("0.1")
        # 先有盤中浮損，再盈利平倉；全球 DD 必須保留中途標記。
        result["equity_points"] = [(t, initial_equity if i == 0 else
                                    initial_equity+profit if i == len(marks)-1 else initial_equity*D("0.8"))
                                   for i, t in enumerate(marks)]
        result["trades"] = [{"opened_at": marks[0], "closed_at": marks[-1], "strategy_mode": Mode.RIGHT,
                             "direction": Direction.LONG, "net_pnl": profit}]
        return result

    def test_global_capital_is_passed_forward_not_rescaled_afterward(self):
        seen = []
        def evaluate(config, warmup, test, *, initial_equity):
            seen.append(initial_equity)
            return self.capital_evaluator(config, warmup, test, initial_equity=initial_equity)
        with tempfile.TemporaryDirectory() as tmp:
            report = run_walk_forward(self.candles, lambda _: dict(self.config), evaluate,
                Path(tmp)/"walk", train_size=3, test_size=2, initial_equity=D(100))
            self.assertEqual(seen, [D(100), D(110), D(121), D("133.1")])
            global_oos = report["global_oos"]
            self.assertEqual(global_oos["final_equity"], D("146.41"))
            self.assertEqual(global_oos["metrics"]["net_pnl"], D("46.41"))
            self.assertEqual(global_oos["metrics"]["maximum_drawdown"], D("0.2"))
            self.assertEqual(global_oos["metrics"]["trades"], 4)
            self.assertEqual(sum(g["trades"] for g in global_oos["trade_groups"].values()), 4)
            times = [t for t, _ in global_oos["equity_points"]]
            self.assertEqual(len(times), len(set(times)))
            self.assertNotIn("NO_GLOBAL_EQUITY_STITCHING", report["limitations"])
            self.assertFalse(report["qualified"])

    def test_evaluator_cannot_reset_carried_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"walk"
            def reset(config, warmup, test, *, initial_equity):
                return self.capital_evaluator(config, warmup, test, initial_equity=D(100))
            with self.assertRaisesRegex(ValueError, "reset"):
                run_walk_forward(self.candles, lambda _: dict(self.config), reset,
                    out, train_size=3, test_size=2, initial_equity=D(100))
            self.assertTrue((out/"fold-0000-result.json").exists())
            self.assertFalse((out/"fold-0001-result.json").exists())
            self.assertFalse((out/"summary.json").exists())

    def test_insolvency_cannot_be_hidden_by_profitable_close(self):
        def ruined(config, warmup, test, *, initial_equity):
            result = self.capital_evaluator(config, warmup, test, initial_equity=initial_equity)
            t, _ = result["equity_points"][1]
            result["equity_points"][1] = (t, D(-1))
            return result
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "insolvency"):
                run_walk_forward(self.candles, lambda _: dict(self.config), ruined,
                    Path(tmp)/"walk", train_size=3, test_size=2, initial_equity=D(100))

    def test_invalid_initial_capital_has_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"walk"
            for value in (D(0), D(-1), D("NaN"), 100.0, True):
                with self.assertRaises(ValueError):
                    run_walk_forward(self.candles, lambda _: dict(self.config), self.capital_evaluator,
                        out, train_size=3, test_size=2, initial_equity=value)
                self.assertFalse(out.exists())

    def test_real_replay_engine_trades_only_test_window_and_carries_equity(self):
        candles = []
        for i in range(1, 201):
            timestamp = self.start+timedelta(minutes=i)
            price = D(2000+i)
            bid = Bar(timestamp, price, price+D(1), price-D(1), price+D("0.9"))
            ask = replace(bid, open=bid.open+D("0.5"), high=bid.high+D("0.5"),
                          low=bid.low+D("0.5"), close=bid.close+D("0.5"))
            candles.append(Candle(bid, ask, True, True, self.start+timedelta(hours=8), D(1), False))
        contract = Contract("SYNTHETIC_GOLD", D(1), D("0.01"), D("0.01"), D(100), D("0.05"), D("0.1"))
        config = {"strategy_version": RiskPolicy().version, "model_version": "NOT_USED", "prompt_version": "NOT_USED"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluate = replay_evaluator(contract, root/"replays")
            result = run_walk_forward(candles, lambda _: dict(config), evaluate, root/"walk",
                                      train_size=180, test_size=10, initial_equity=D(10000))
            self.assertGreater(result["global_oos"]["metrics"]["trades"], 0)
            self.assertEqual(result["folds"][1]["initial_equity"],
                             D(10000)+result["folds"][0]["metrics"]["net_pnl"])
            for file in (root/"walk").glob("*-result.json"):
                record = json.loads(file.read_text(encoding="utf-8"))
                self.assertEqual(record["result"]["replay_type"], "BAR_REPLAY_ONLY")
                for trade in record["result"]["trades"]:
                    self.assertGreaterEqual(trade["opened_at"], record["test_start"])
                    self.assertLessEqual(trade["closed_at"], record["test_end"])
            with self.assertRaisesRegex(ValueError, "exact frozen"):
                evaluate({**config, "ignored_parameter": 1}, candles[:180], candles[180:190], initial_equity=D(10000))
