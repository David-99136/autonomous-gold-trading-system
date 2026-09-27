import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from gold_system.core import *
from gold_system.risk import make_plan
from gold_system.broker import PaperBroker
from gold_system.engine import Engine
from gold_system.store import Store
from gold_system.analysis import evidence_allowed, analyze, continuous


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "audit.db")
        self.now = datetime(2026, 1, 5, 14, tzinfo=timezone.utc)
        self.policy = RiskPolicy()
        self.contract = Contract("TEST", D(1), D("0.01"), D("0.01"), D(100), D("0.01"), D("0.1"))
        self.q = Quote(self.now, D(2000), D("2000.5"))
        self.quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        self.s = Signal(
            signal_id="one", version=self.policy.version,
            created=self.now, expires=self.now + timedelta(minutes=1),
            direction=Direction.LONG, mode=Mode.RIGHT,
            stop=D(1990), target=D(2030), reason="test",
        )
        self.broker = PaperBroker()
        self.engine = Engine(self.store, self.broker, self.contract, self.policy)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, signal=None, quality=None, quote=None):
        return make_plan(signal or self.s, quote or self.q, quality or self.quality,
                         self.contract, self.policy, D(10000), self.now)

    def tick(self, signal=None, quote=None, quality=None):
        asyncio.run(self.engine.tick(self.now, quote or self.q, quality or self.quality, signal))

    def test_size_respects_risk_margin_and_half_step(self):
        p = self.plan()
        self.assertLessEqual(p.risk, D(25))
        self.assertLessEqual(p.margin, D(2000))
        self.assertEqual((p.size / 2) % self.contract.increment, 0)

    def test_operator_stop_persists_across_connections_and_blocks_entry(self):
        control = Store(Path(self.tmp.name) / "audit.db")
        try:
            control.stop_new_entries()
        finally:
            control.close()
        self.tick(self.s)
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())
        self.assertEqual(self.store.events()[-1]["payload"]["reason"], "OPERATOR_STOP_NEW")

    def test_operator_stop_does_not_disable_hard_stop_management(self):
        self.tick(self.s)
        self.assertIsNotNone(self.broker.position)
        self.store.stop_new_entries()
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_history_fault_blocks_new_position(self):
        from gold_system.history_quality import HistoryQualityMonitor
        HistoryQualityMonitor(self.store).observe({}, start=self.now, end=self.now, received_at=self.now)
        self.tick(self.s)
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_history_fault_does_not_disable_existing_stop(self):
        from gold_system.history_quality import HistoryQualityMonitor
        self.tick(self.s)
        self.assertIsNotNone(self.broker.position)
        HistoryQualityMonitor(self.store).observe({}, start=self.now, end=self.now, received_at=self.now)
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_offline_import_failure_blocks_new_position(self):
        from gold_system.history_import import audit_file
        result, code = audit_file(Path(self.tmp.name) / "audit.db", Path(self.tmp.name) / "missing.json",
                                 start=self.now.isoformat(), end=self.now.isoformat(), received_at=self.now.isoformat())
        self.assertEqual(code, 1)
        self.assertTrue(result["lock_persisted"])
        self.tick(self.s)
        self.assertIsNone(self.broker.position)

    def test_offline_import_failure_preserves_existing_hard_stop(self):
        from gold_system.history_import import audit_file
        self.tick(self.s)
        self.assertIsNotNone(self.broker.position)
        audit_file(Path(self.tmp.name) / "audit.db", Path(self.tmp.name) / "missing.json",
                   start=self.now.isoformat(), end=self.now.isoformat(), received_at=self.now.isoformat())
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_operator_stop_corrupt_value_is_fail_closed(self):
        self.store.set("operator_stop_new", "false")
        self.tick(self.s)
        self.assertIsNone(self.broker.position)

    def test_operator_stop_audit_failure_rolls_back_state(self):
        self.store.db.execute("CREATE TRIGGER fail_control BEFORE INSERT ON events "
                              "WHEN NEW.kind='OPERATOR_STOP_NEW' BEGIN SELECT RAISE(ABORT, 'test'); END")
        self.store.db.commit()
        with self.assertRaises(Exception):
            self.store.stop_new_entries()
        self.assertFalse(self.store.entries_stopped())

    def test_quality_fail_closed(self):
        cases = [dict(tradeable=False), dict(liquid_window=False), dict(data_complete=False),
                 dict(extreme_volatility=True), dict(clock_offset_ms=1001), dict(minutes_to_boundary=D(30)),
                 dict(spread_limit=D("0.1")), dict(hourly_range=D(4)), dict(analysis_available=False),
                 dict(budget_available=False)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Rejected):
                self.plan(quality=replace(self.quality, **case))

    def test_trend_exception_not_extreme_exception(self):
        self.plan(quality=replace(self.quality, hourly_range=D(4), trend_exception=True))
        with self.assertRaises(Rejected):
            self.plan(quality=replace(self.quality, extreme_volatility=True, trend_exception=True))

    def test_expiry_stale_future_and_version(self):
        for signal in (replace(self.s, expires=self.now), replace(self.s, version="changed"),
                       replace(self.s, created=self.now + timedelta(seconds=1))):
            with self.assertRaises(Rejected):
                self.plan(signal=signal)
        with self.assertRaises(Rejected):
            self.plan(quote=replace(self.q, timestamp=self.now - timedelta(seconds=3)))

    def test_invalid_stop_rr_and_nan(self):
        for s in (replace(self.s, stop=D(2001)), replace(self.s, target=D(2005)),
                  replace(self.s, stop=D("NaN"))):
            with self.assertRaises(Rejected):
                self.plan(signal=s)

    def test_short_sizing(self):
        p = self.plan(signal=replace(self.s, direction=Direction.SHORT, stop=D(2010), target=D(1970)))
        self.assertGreater(p.size, 0)

    def test_partial_and_stop_tightening(self):
        self.tick(self.s)
        original = self.broker.position.size
        self.tick(quote=Quote(self.now, D(2012), D("2012.5")))
        self.assertEqual(self.broker.position.size, original / 2)
        self.assertGreater(self.engine.realized, 0)
        with self.assertRaises(Rejected):
            self.broker.tighten(D(1990))

    def test_session_close_even_without_analysis(self):
        self.tick(self.s)
        self.tick(quality=replace(self.quality, minutes_to_boundary=D(10), analysis_available=False))
        self.assertIsNone(self.broker.position)

    def test_cost_ledger_blocks_entries_but_not_stop_management(self):
        from gold_system.costs import CostLedger
        self.tick(self.s)
        self.engine.cost_ledger = CostLedger(self.store)
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.tick(replace(self.s, signal_id="new"))
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.store.events()[-1]["payload"]["reason"], "BUDGET_EXHAUSTED")

    def test_cost_database_failure_does_not_prevent_session_exit(self):
        from unittest.mock import Mock
        self.tick(self.s)
        ledger = Mock()
        ledger.status.side_effect = RuntimeError("broken ledger")
        self.engine.cost_ledger = ledger
        self.tick(quality=replace(self.quality, minutes_to_boundary=D(10)))
        self.assertIsNone(self.broker.position)
        ledger.status.assert_not_called()
        self.tick(replace(self.s, signal_id="next"))
        self.assertEqual(self.store.events()[-1]["payload"]["reason"], "BUDGET_STATE_UNAVAILABLE")

    def test_soft_realized_latch_and_not_floating(self):
        self.tick(self.s)
        self.assertFalse(self.engine.soft)
        self.broker.close(self.q)
        self.engine.realized = D(-300)
        self.tick(replace(self.s, signal_id="two"))
        self.assertTrue(self.engine.soft)
        self.assertEqual(self.broker.submissions, 1)
        self.engine.realized = D(0)
        self.tick(replace(self.s, signal_id="three"))
        self.assertTrue(self.engine.soft)

    def test_hard_lock_survives_restart(self):
        self.tick(self.s)
        self.engine.realized = D(-1000)
        self.tick()
        self.assertIsNone(self.broker.position)
        restarted = Engine(self.store, self.broker, self.contract, self.policy)
        self.assertTrue(restarted.hard)
        self.assertNotEqual(restarted.state, "PAPER_READY")

    def test_duplicate_after_closed(self):
        self.tick(self.s)
        self.broker.close(self.q)
        self.tick(self.s)
        self.assertEqual(self.broker.submissions, 1)

    def test_unknown_outcome_locks_without_retry(self):
        def timeout_after_open(plan, contract):
            PaperBroker.open(self.broker, plan, contract)
            raise TimeoutError()
        self.broker.open = timeout_after_open
        self.tick(self.s)
        self.tick(self.s)
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.engine.state, "RECOVERY_LOCKED")

    def test_secret_redaction(self):
        self.store.emit("TEST", nested={"api_key": "sensitive", "password": "sensitive"})
        self.assertNotIn("sensitive", json.dumps(self.store.events()))

    def test_evidence_threshold(self):
        fields = dict(source="official", released_at="t", received_at="t", actual=0, a=1, b=2, c=3)
        weights = {k: D(1) for k in "abcde"}
        self.assertFalse(evidence_allowed(fields, weights))
        fields["d"] = 4
        self.assertTrue(evidence_allowed(fields, weights))
        fields["actual"] = None
        self.assertFalse(evidence_allowed(fields, weights))

    def test_missing_analysis_no_trade(self):
        self.assertEqual(analyze([], [], [], self.now).direction, Direction.NO_TRADE)

    def test_bar_future_and_gaps(self):
        b = Bar(self.now, D(2), D(3), D(1), D(2))
        self.assertTrue(continuous([b], 5, self.now))
        self.assertFalse(continuous([replace(b, timestamp=self.now - timedelta(minutes=10)), b], 5, self.now))
        self.assertFalse(continuous([replace(b, timestamp=self.now + timedelta(minutes=1))], 5, self.now))

    # [Antigravity | 2026-09-27] Signal 契約新增測試（工作包 4 前半）────────────────────────────────────

    def _base_signal(self, **overrides):
        """回傳含完整新欄位的 Signal；以 dataclasses.replace 覆蓋指定欄位，避免重複關鍵字錯誤。"""
        base = Signal(
            signal_id="contract-test", version=self.policy.version,
            created=self.now, expires=self.now + timedelta(minutes=1),
            direction=Direction.LONG, mode=Mode.RIGHT,
            stop=D(1990), target=D(2030), reason="test",
            zone_id="H1-ZONE-001",
            confirmation_condition="5m bullish engulfing above demand zone",
            invalidation_price=D("1985"),
            rule_score=D("0.75"),
            confidence=D("0.60"),
            data_completeness="COMPLETE",
        )
        return replace(base, **overrides)

    def test_signal_with_all_new_fields_can_be_created(self):
        """Signal 有完整欄位可以建立，欄位值正確保存。"""
        s = self._base_signal()
        self.assertEqual(s.zone_id, "H1-ZONE-001")
        self.assertEqual(s.confirmation_condition, "5m bullish engulfing above demand zone")
        self.assertEqual(s.invalidation_price, D("1985"))
        self.assertEqual(s.rule_score, D("0.75"))
        self.assertEqual(s.confidence, D("0.60"))
        self.assertEqual(s.data_completeness, "COMPLETE")

    def test_signal_rule_score_out_of_range_raises(self):
        """rule_score 超出 [0,1] 或非有限數時，Signal 建立失敗。"""
        with self.assertRaises(ValueError):
            self._base_signal(rule_score=D("1.01"))
        with self.assertRaises(ValueError):
            self._base_signal(rule_score=D("-0.01"))
        with self.assertRaises(ValueError):
            self._base_signal(rule_score=D("Infinity"))
        with self.assertRaises(ValueError):
            self._base_signal(rule_score=D("NaN"))

    def test_signal_confidence_out_of_range_raises(self):
        """confidence 若非 None，超出 [0,1] 時失敗；None 應通過。"""
        # None 是合法值（尚未校準）
        s = self._base_signal(confidence=None)
        self.assertIsNone(s.confidence)
        # 邊界值 0 和 1 合法
        self._base_signal(confidence=D("0"))
        self._base_signal(confidence=D("1"))
        # 超出邊界
        with self.assertRaises(ValueError):
            self._base_signal(confidence=D("1.001"))
        with self.assertRaises(ValueError):
            self._base_signal(confidence=D("-0.001"))
        with self.assertRaises(ValueError):
            self._base_signal(confidence=D("Infinity"))

    def test_signal_invalid_data_completeness_raises(self):
        """data_completeness 非允許值時失敗；三個允許值均應通過。"""
        for valid in ("COMPLETE", "PARTIAL", "INSUFFICIENT"):
            self._base_signal(data_completeness=valid)  # 不應拋例外
        with self.assertRaises(ValueError):
            self._base_signal(data_completeness="UNKNOWN")
        with self.assertRaises(ValueError):
            self._base_signal(data_completeness="complete")  # 大小寫敏感
        with self.assertRaises(ValueError):
            self._base_signal(data_completeness="")

    def test_signal_asdict_compatible(self):
        """Signal 與 dataclasses.asdict 相容（供 codex_analysis.py 中的 asdict(s) 使用）。"""
        s = self._base_signal()
        d = asdict(s)
        # 原有欄位保持不變
        self.assertEqual(d["signal_id"], "contract-test")
        self.assertEqual(d["direction"], Direction.LONG)
        # 新欄位出現在 dict 中
        self.assertIn("zone_id", d)
        self.assertIn("confirmation_condition", d)
        self.assertIn("invalidation_price", d)
        self.assertIn("rule_score", d)
        self.assertIn("confidence", d)
        self.assertIn("data_completeness", d)
        self.assertEqual(d["data_completeness"], "COMPLETE")
        self.assertEqual(d["rule_score"], D("0.75"))


if __name__ == "__main__":
    unittest.main()


    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, signal=None, quality=None, quote=None):
        return make_plan(signal or self.s, quote or self.q, quality or self.quality,
                         self.contract, self.policy, D(10000), self.now)

    def tick(self, signal=None, quote=None, quality=None):
        asyncio.run(self.engine.tick(self.now, quote or self.q, quality or self.quality, signal))

    def test_size_respects_risk_margin_and_half_step(self):
        p = self.plan()
        self.assertLessEqual(p.risk, D(25))
        self.assertLessEqual(p.margin, D(2000))
        self.assertEqual((p.size / 2) % self.contract.increment, 0)

    def test_operator_stop_persists_across_connections_and_blocks_entry(self):
        control = Store(Path(self.tmp.name) / "audit.db")
        try:
            control.stop_new_entries()
        finally:
            control.close()
        self.tick(self.s)
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())
        self.assertEqual(self.store.events()[-1]["payload"]["reason"], "OPERATOR_STOP_NEW")

    def test_operator_stop_does_not_disable_hard_stop_management(self):
        self.tick(self.s)
        self.assertIsNotNone(self.broker.position)
        self.store.stop_new_entries()
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_history_fault_blocks_new_position(self):
        from gold_system.history_quality import HistoryQualityMonitor
        HistoryQualityMonitor(self.store).observe({}, start=self.now, end=self.now, received_at=self.now)
        self.tick(self.s)
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_history_fault_does_not_disable_existing_stop(self):
        from gold_system.history_quality import HistoryQualityMonitor
        self.tick(self.s)
        self.assertIsNotNone(self.broker.position)
        HistoryQualityMonitor(self.store).observe({}, start=self.now, end=self.now, received_at=self.now)
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_offline_import_failure_blocks_new_position(self):
        from gold_system.history_import import audit_file
        result, code = audit_file(Path(self.tmp.name) / "audit.db", Path(self.tmp.name) / "missing.json",
                                 start=self.now.isoformat(), end=self.now.isoformat(), received_at=self.now.isoformat())
        self.assertEqual(code, 1)
        self.assertTrue(result["lock_persisted"])
        self.tick(self.s)
        self.assertIsNone(self.broker.position)

    def test_offline_import_failure_preserves_existing_hard_stop(self):
        from gold_system.history_import import audit_file
        self.tick(self.s)
        self.assertIsNotNone(self.broker.position)
        audit_file(Path(self.tmp.name) / "audit.db", Path(self.tmp.name) / "missing.json",
                   start=self.now.isoformat(), end=self.now.isoformat(), received_at=self.now.isoformat())
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.assertTrue(self.store.entries_stopped())

    def test_operator_stop_corrupt_value_is_fail_closed(self):
        self.store.set("operator_stop_new", "false")
        self.tick(self.s)
        self.assertIsNone(self.broker.position)

    def test_operator_stop_audit_failure_rolls_back_state(self):
        self.store.db.execute("CREATE TRIGGER fail_control BEFORE INSERT ON events "
                              "WHEN NEW.kind='OPERATOR_STOP_NEW' BEGIN SELECT RAISE(ABORT, 'test'); END")
        self.store.db.commit()
        with self.assertRaises(Exception):
            self.store.stop_new_entries()
        self.assertFalse(self.store.entries_stopped())

    def test_quality_fail_closed(self):
        cases = [dict(tradeable=False), dict(liquid_window=False), dict(data_complete=False),
                 dict(extreme_volatility=True), dict(clock_offset_ms=1001), dict(minutes_to_boundary=D(30)),
                 dict(spread_limit=D("0.1")), dict(hourly_range=D(4)), dict(analysis_available=False),
                 dict(budget_available=False)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Rejected):
                self.plan(quality=replace(self.quality, **case))

    def test_trend_exception_not_extreme_exception(self):
        self.plan(quality=replace(self.quality, hourly_range=D(4), trend_exception=True))
        with self.assertRaises(Rejected):
            self.plan(quality=replace(self.quality, extreme_volatility=True, trend_exception=True))

    def test_expiry_stale_future_and_version(self):
        for signal in (replace(self.s, expires=self.now), replace(self.s, version="changed"),
                       replace(self.s, created=self.now + timedelta(seconds=1))):
            with self.assertRaises(Rejected):
                self.plan(signal=signal)
        with self.assertRaises(Rejected):
            self.plan(quote=replace(self.q, timestamp=self.now - timedelta(seconds=3)))

    def test_invalid_stop_rr_and_nan(self):
        for s in (replace(self.s, stop=D(2001)), replace(self.s, target=D(2005)),
                  replace(self.s, stop=D("NaN"))):
            with self.assertRaises(Rejected):
                self.plan(signal=s)

    def test_short_sizing(self):
        p = self.plan(signal=replace(self.s, direction=Direction.SHORT, stop=D(2010), target=D(1970)))
        self.assertGreater(p.size, 0)

    def test_partial_and_stop_tightening(self):
        self.tick(self.s)
        original = self.broker.position.size
        self.tick(quote=Quote(self.now, D(2012), D("2012.5")))
        self.assertEqual(self.broker.position.size, original / 2)
        self.assertGreater(self.engine.realized, 0)
        with self.assertRaises(Rejected):
            self.broker.tighten(D(1990))

    def test_session_close_even_without_analysis(self):
        self.tick(self.s)
        self.tick(quality=replace(self.quality, minutes_to_boundary=D(10), analysis_available=False))
        self.assertIsNone(self.broker.position)

    def test_cost_ledger_blocks_entries_but_not_stop_management(self):
        from gold_system.costs import CostLedger
        self.tick(self.s)
        self.engine.cost_ledger = CostLedger(self.store)
        self.tick(quote=Quote(self.now, D(1989), D("1989.5")))
        self.assertIsNone(self.broker.position)
        self.tick(replace(self.s, signal_id="new"))
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.store.events()[-1]["payload"]["reason"], "BUDGET_EXHAUSTED")

    def test_cost_database_failure_does_not_prevent_session_exit(self):
        from unittest.mock import Mock
        self.tick(self.s)
        ledger = Mock()
        ledger.status.side_effect = RuntimeError("broken ledger")
        self.engine.cost_ledger = ledger
        self.tick(quality=replace(self.quality, minutes_to_boundary=D(10)))
        self.assertIsNone(self.broker.position)
        ledger.status.assert_not_called()
        self.tick(replace(self.s, signal_id="next"))
        self.assertEqual(self.store.events()[-1]["payload"]["reason"], "BUDGET_STATE_UNAVAILABLE")

    def test_soft_realized_latch_and_not_floating(self):
        self.tick(self.s)
        self.assertFalse(self.engine.soft)
        self.broker.close(self.q)
        self.engine.realized = D(-300)
        self.tick(replace(self.s, signal_id="two"))
        self.assertTrue(self.engine.soft)
        self.assertEqual(self.broker.submissions, 1)
        self.engine.realized = D(0)
        self.tick(replace(self.s, signal_id="three"))
        self.assertTrue(self.engine.soft)

    def test_hard_lock_survives_restart(self):
        self.tick(self.s)
        self.engine.realized = D(-1000)
        self.tick()
        self.assertIsNone(self.broker.position)
        restarted = Engine(self.store, self.broker, self.contract, self.policy)
        self.assertTrue(restarted.hard)
        self.assertNotEqual(restarted.state, "PAPER_READY")

    def test_duplicate_after_closed(self):
        self.tick(self.s)
        self.broker.close(self.q)
        self.tick(self.s)
        self.assertEqual(self.broker.submissions, 1)

    def test_unknown_outcome_locks_without_retry(self):
        def timeout_after_open(plan, contract):
            PaperBroker.open(self.broker, plan, contract)
            raise TimeoutError()
        self.broker.open = timeout_after_open
        self.tick(self.s)
        self.tick(self.s)
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.engine.state, "RECOVERY_LOCKED")

    def test_secret_redaction(self):
        self.store.emit("TEST", nested={"api_key": "sensitive", "password": "sensitive"})
        self.assertNotIn("sensitive", json.dumps(self.store.events()))

    def test_evidence_threshold(self):
        fields = dict(source="official", released_at="t", received_at="t", actual=0, a=1, b=2, c=3)
        weights = {k: D(1) for k in "abcde"}
        self.assertFalse(evidence_allowed(fields, weights))
        fields["d"] = 4
        self.assertTrue(evidence_allowed(fields, weights))
        fields["actual"] = None
        self.assertFalse(evidence_allowed(fields, weights))

    def test_missing_analysis_no_trade(self):
        self.assertEqual(analyze([], [], [], self.now).direction, Direction.NO_TRADE)

    def test_bar_future_and_gaps(self):
        b = Bar(self.now, D(2), D(3), D(1), D(2))
        self.assertTrue(continuous([b], 5, self.now))
        self.assertFalse(continuous([replace(b, timestamp=self.now - timedelta(minutes=10)), b], 5, self.now))
        self.assertFalse(continuous([replace(b, timestamp=self.now + timedelta(minutes=1))], 5, self.now))


if __name__ == "__main__":
    unittest.main()
