"""候選訊號管線與完整契約產出測試。

[Author: Antigravity | Date: 2026-09-27]
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import AsyncMock

from gold_system.analysis import analyze, candidate_signals, frame_candidates, zones
from gold_system.core import Bar, Contract, D, Direction, Mode, Quality, Quote, RiskPolicy, Signal
from gold_system.broker import PaperBroker
from gold_system.engine import Engine
from gold_system.service import MarketFrame
from gold_system.codex_analysis import CodexFrameAnalysis
from gold_system.store import Store


def _bar(stamp, o, h, l, c):
    return Bar(stamp, D(str(o)), D(str(h)), D(str(l)), D(str(c)))


class CandidatePipelineTests(unittest.TestCase):
    # [Codex | 2026-09-27] ID 必須追蹤風險參數，而非僅追蹤當下價格。
    def test_candidate_identity_changes_with_risk_evidence(self):
        h1, m5, m1 = self._make_series()
        before = candidate_signals(h1, m5, m1, self.now)
        self.assertEqual(before, candidate_signals(tuple(h1), tuple(m5), tuple(m1), self.now))
        m5[-3] = replace(m5[-3], low=m5[-3].low - D(1))
        after = candidate_signals(h1, m5, m1, self.now)
        self.assertNotEqual(before[0].stop, after[0].stop)
        self.assertNotEqual(before[0].signal_id, after[0].signal_id)

    def setUp(self):
        self.now = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
        self.version = "test-v1"

    def _make_series(self, base_price=D(2000), trend="bullish"):
        """建立連續合格的 H1(3根)、M5(12根)、M1(2根) K 棒。"""
        # H1: 3 根連續 K 棒，結尾在 self.now (60 分鐘連續)
        h1 = []
        for i in range(3):
            t = self.now - timedelta(hours=2 - i)
            if trend == "bullish":
                p = base_price + D(i * 10)
                h1.append(_bar(t, p, p + D(5), p - D(2), p + D(4)))
            elif trend == "bearish":
                p = base_price - D(i * 10)
                h1.append(_bar(t, p, p + D(2), p - D(5), p - D(4)))
            else:
                p = base_price
                h1.append(_bar(t, p, p + D(2), p - D(2), p))

        # M5: 12 根連續 K 棒，結尾在 self.now (5 分鐘連續)
        m5 = []
        for i in range(12):
            t = self.now - timedelta(minutes=(11 - i) * 5)
            if trend == "bullish":
                p = base_price + D(20) + D(i)
                m5.append(_bar(t, p, p + D("0.8"), p - D("0.3"), p + D("0.5")))
            elif trend == "bearish":
                p = base_price - D(20) - D(i)
                m5.append(_bar(t, p, p + D("0.3"), p - D("0.8"), p - D("0.5")))
            else:
                p = base_price
                m5.append(_bar(t, p, p + D("0.5"), p - D("0.5"), p))

        # M1: 2 根連續 K 棒，結尾在 self.now (1 分鐘連續)
        m1 = []
        for i in range(2):
            t = self.now - timedelta(minutes=1 - i)
            if trend == "bullish":
                p = base_price + D(30) + D(i)
                m1.append(_bar(t, p, p + D("0.4"), p - D("0.1"), p + D("0.3")))
            elif trend == "bearish":
                p = base_price - D(30) - D(i)
                m1.append(_bar(t, p, p + D("0.1"), p - D("0.4"), p - D("0.3")))
            else:
                p = base_price
                m1.append(_bar(t, p, p + D("0.2"), p - D("0.2"), p))

        return h1, m5, m1

    def test_analyze_incomplete_data_returns_full_contract_defaults(self):
        sig = analyze([], [], [], self.now, version=self.version)
        self.assertEqual(sig.direction, Direction.NO_TRADE)
        self.assertEqual(sig.reason, "DATA_INCOMPLETE")
        self.assertEqual(sig.data_completeness, "INSUFFICIENT")
        self.assertEqual(sig.rule_score, D(0))
        self.assertEqual(sig.invalidation_price, D(0))
        self.assertEqual(sig.zone_id, "")
        self.assertEqual(sig.confirmation_condition, "")
        self.assertIsNone(sig.confidence)

    def test_analyze_unconfirmed_structure_reports_complete_data(self):
        h1, m5, m1 = self._make_series(trend="flat")
        sig = analyze(h1, m5, m1, self.now, version=self.version)
        self.assertEqual(sig.direction, Direction.NO_TRADE)
        self.assertEqual(sig.reason, "STRUCTURE_UNCONFIRMED")
        self.assertEqual(sig.data_completeness, "COMPLETE")
        self.assertEqual(sig.rule_score, D(0))
        self.assertEqual(sig.invalidation_price, D(0))

    def test_analyze_right_continuation_populates_contract_fields(self):
        h1, m5, m1 = self._make_series(trend="bullish")
        sig = analyze(h1, m5, m1, self.now, version=self.version)
        self.assertEqual(sig.direction, Direction.LONG)
        self.assertEqual(sig.mode, Mode.RIGHT)
        self.assertEqual(sig.data_completeness, "COMPLETE")
        self.assertEqual(sig.rule_score, D("0.70"))
        self.assertEqual(sig.invalidation_price, sig.stop)
        self.assertTrue(sig.zone_id.startswith("TREND_H1_BULL_"))
        self.assertEqual(sig.confirmation_condition, "M5_BREAKOUT_M1_TRIGGER_LONG")
        self.assertIsNone(sig.confidence)
        # R:R 必須至少 1.5 倍
        self.assertGreaterEqual(abs(sig.target - m1[-1].close), abs(m1[-1].close - sig.stop) * D("1.5"))

    def test_candidate_signals_empty_on_incomplete_history(self):
        self.assertEqual(candidate_signals([], [], [], self.now), ())

    def test_candidate_signals_produces_valid_signals(self):
        h1, m5, m1 = self._make_series(trend="bullish")
        candidates = candidate_signals(h1, m5, m1, self.now, version=self.version)
        self.assertGreaterEqual(len(candidates), 1)
        for s in candidates:
            self.assertIsInstance(s, Signal)
            self.assertIn(s.direction, (Direction.LONG, Direction.SHORT))
            self.assertEqual(s.data_completeness, "COMPLETE")
            self.assertEqual(s.invalidation_price, s.stop)
            self.assertGreater(s.rule_score, D(0))
            self.assertTrue(bool(s.zone_id))
            self.assertTrue(bool(s.confirmation_condition))

    def test_frame_candidates_adapter(self):
        h1, m5, m1 = self._make_series(trend="bullish")
        quote = Quote(self.now, D(2030), D("2030.5"))
        quality = Quality(tradeable=True, liquid_window=True, data_complete=True)
        frame = MarketFrame(quote, quality, tuple(h1), tuple(m5), tuple(m1))
        candidates = frame_candidates(frame, version=self.version)
        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual(candidates[0].version, self.version)

    def test_frame_candidates_empty_on_missing_quote_or_bars(self):
        self.assertEqual(frame_candidates(None), ())
        self.assertEqual(frame_candidates(MarketFrame(Quote(self.now, D(2000), D(2001)), Quality())), ())

    # [Codex | 2026-09-27] 完整根數不等於資料合格；上游品質故障必須保留。
    def test_frame_quality_fault_and_unaligned_close_produce_no_candidates(self):
        h1, m5, m1 = self._make_series()
        frame = MarketFrame(Quote(self.now, D(2031), D("2031.1")), Quality(data_complete=False),
                            tuple(h1), tuple(m5), tuple(m1))
        self.assertEqual(frame_candidates(frame), ())
        shifted = [replace(b, timestamp=b.timestamp - timedelta(seconds=1)) for b in m5]
        self.assertEqual(candidate_signals(h1, shifted, m1, self.now), ())

    # [Codex | 2026-09-27] 合成需求區反轉，不把策略判斷 mock 成固定訊號。
    def _left_series(self, short=False):
        h1, m5, m1 = self._make_series()
        h1[-1] = _bar(self.now, 2011, 2013, 2008, 2010)
        m5[-2] = _bar(m5[-2].timestamp, 2003, 2008, 2000, 2003)
        m5[-1] = _bar(self.now, 2008, 2012, 2007, 2010)
        m1[-2] = _bar(m1[-2].timestamp, 2007, 2009, 2006, 2008)
        m1[-1] = _bar(self.now, 2009, 2013, 2008, 2011)
        if short:
            def mirror(b):
                return _bar(b.timestamp, D(4000)-b.open, D(4000)-b.low,
                            D(4000)-b.high, D(4000)-b.close)
            h1, m5, m1 = ([mirror(b) for b in series] for series in (h1, m5, m1))
        return h1, m5, m1

    def test_left_reversals_have_complete_contract_and_reproducible_identity(self):
        for short in (False, True):
            with self.subTest(short=short):
                h1, m5, m1 = self._left_series(short)
                candidates = candidate_signals(h1, m5, m1, self.now)
                self.assertEqual(len(candidates), 1)
                signal = candidates[0]
                self.assertEqual(signal.mode, Mode.LEFT)
                self.assertEqual(signal.direction, Direction.SHORT if short else Direction.LONG)
                self.assertEqual(signal.data_completeness, "COMPLETE")
                self.assertEqual(signal.rule_score, D("0.80"))
                self.assertEqual(signal.invalidation_price, signal.stop)
                self.assertTrue(signal.zone_id and signal.confirmation_condition)
                self.assertIsNone(signal.confidence)
                self.assertEqual(analyze(h1, m5, m1, self.now).mode, Mode.LEFT)
                self.assertEqual(candidates, candidate_signals(h1, m5, m1, self.now))
                m1[-1] = replace(m1[-1], close=m1[-1].close + signal.direction.sign)
                changed = candidate_signals(h1, m5, m1, self.now)[0]
                self.assertNotEqual(signal.signal_id, changed.signal_id)

    def test_gaps_future_and_stale_bars_cannot_create_candidates(self):
        for index, step in enumerate((60, 5, 1)):
            for fault in ("gap", "future", "stale", "missing"):
                with self.subTest(timeframe=step, fault=fault):
                    series = list(self._make_series())
                    bars = series[index]
                    if fault == "gap":
                        bars[0] = replace(bars[0], timestamp=bars[0].timestamp-timedelta(minutes=step))
                    elif fault == "future":
                        bars[-1] = replace(bars[-1], timestamp=self.now+timedelta(minutes=step))
                    elif fault == "missing":
                        bars.pop(0)
                    else:
                        series[index] = [replace(b, timestamp=b.timestamp-timedelta(minutes=step)) for b in bars]
                    self.assertEqual(candidate_signals(*series, self.now), ())
                    self.assertEqual(analyze(*series, self.now).data_completeness, "INSUFFICIENT")


class CodexBridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + "/test.db")
        self.now = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
        self.policy = RiskPolicy()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_codex_frame_analysis_with_frame_candidates(self):
        # 建立具備候選訊號的 MarketFrame
        h1 = [_bar(self.now - timedelta(hours=2 - i), D(2000 + i * 10), D(2005 + i * 10), D(1998 + i * 10), D(2004 + i * 10)) for i in range(3)]
        # [Codex | 2026-09-27] 金額從字串建 Decimal，避免測試引入 float 誤差。
        m5 = [_bar(self.now - timedelta(minutes=(11 - i) * 5), D(2020 + i), D("2020.8") + i, D("2019.7") + i, D("2020.5") + i) for i in range(12)]
        m1 = [_bar(self.now - timedelta(minutes=1 - i), D(2030 + i), D("2030.4") + i, D("2029.9") + i, D("2030.3") + i) for i in range(2)]
        quote = Quote(self.now, D(2031), D("2031.5"))
        quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        frame = MarketFrame(quote, quality, tuple(h1), tuple(m5), tuple(m1))

        # 取得候選
        candidates = frame_candidates(frame, version=self.policy.version)
        self.assertTrue(len(candidates) >= 1)
        chosen_id = candidates[0].signal_id

        # 模擬 Codex runner 選擇候選
        runner = AsyncMock()
        runner.model = "test-model"
        runner.select.return_value = {
            "candidate_id": chosen_id,
            "confidence": 0.85,
            "reason_code": "EVIDENCE_SUPPORTS",
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20},
        }

        analysis = CodexFrameAnalysis(
            runner, frame_candidates,
            AsyncMock(return_value={"evidence_eligible": True}),
            self.store, max_calls=5, clock=lambda: self.now,
        )

        chosen_signal = await analysis(frame)
        self.assertEqual(chosen_signal.signal_id, chosen_id)
        self.assertEqual(chosen_signal.direction, candidates[0].direction)
        self.assertEqual(chosen_signal.data_completeness, "COMPLETE")
        self.assertEqual(chosen_signal.rule_score, D("0.70"))
        self.assertTrue(bool(chosen_signal.confirmation_condition))
        self.assertTrue(bool(chosen_signal.zone_id))

        # [Codex | 2026-09-27] 真實風控／引擎／PaperBroker 串接，不等同券商成交驗收。
        contract = Contract("GOLD", D(1), D("0.01"), D("0.01"), D(100), D("0.05"), D("0.05"))
        broker = PaperBroker()
        engine = Engine(self.store, broker, contract, self.policy)
        await engine.tick(self.now, frame.quote, frame.quality, chosen_signal)
        self.assertIsNotNone(broker.position)
        self.assertEqual(broker.submissions, 1)
        self.assertEqual(broker.position.stop, chosen_signal.stop)
        self.assertTrue(any(e["kind"] == "ENTRY" for e in self.store.events()))
        # 壞的新訊號不能妨礙原部位硬停損。
        bad = replace(chosen_signal, signal_id="bad", data_completeness="INSUFFICIENT")
        stop_quote = Quote(self.now, D(2028), D("2028.1"))
        await engine.tick(self.now, stop_quote, replace(quality, data_complete=False), bad)
        self.assertIsNone(broker.position)
        self.assertEqual(broker.submissions, 1)


if __name__ == "__main__":
    unittest.main()
