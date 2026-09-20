import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gold_system.analysis_journal import AnalysisJournal
from gold_system.codex_analysis import CodexFrameAnalysis
from gold_system.core import D, Direction, Mode, Signal
from gold_system.store import Store


class AnalysisJournalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "audit.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.signal = Signal("candidate", "strategy-v1", self.now, self.now + timedelta(minutes=1),
                             Direction.LONG, Mode.RIGHT, D(1990), D(2020), "fixture")
        self.args = dict(frame=None, candidates=(self.signal,), evidence={"evidence_eligible": True},
                         model="test-model", prompt_version="prompt-v1")
        self.result = dict(candidate_id="candidate", confidence=0.5, reason_code="EVIDENCE_SUPPORTS",
                           usage={"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10})

    def journal(self, cap=2, store=None):
        return AnalysisJournal(store or self.store, scope="fixture-budget", max_calls=cap)

    def test_duplicate_input_consumes_only_one_reservation(self):
        journal = self.journal()
        key = journal.reserve(**self.args)
        self.assertIsNotNone(key)
        self.assertIsNone(journal.reserve(**self.args))
        self.assertEqual(journal.used, 1)

    def test_restart_preserves_cap(self):
        first = self.journal(cap=1)
        first.reserve(**self.args)
        other = Store(self.path)
        try:
            resumed = self.journal(cap=1, store=other)
            changed = dict(self.args, evidence={"evidence_eligible": True, "revision": 2})
            self.assertIsNone(resumed.reserve(**changed))
            self.assertEqual(resumed.used, 1)
        finally:
            other.close()

    def test_new_instance_cannot_silently_change_cap(self):
        self.journal(cap=1)
        with self.assertRaisesRegex(ValueError, "BUDGET_CHANGE"):
            self.journal(cap=2)

    def test_model_prompt_strategy_changes_are_rejected(self):
        journal = self.journal()
        journal.reserve(**self.args)
        for changed in (dict(model="other"), dict(prompt_version="v2"),
                        dict(candidates=(replace(self.signal, version="strategy-v2"),))):
            with self.assertRaisesRegex(ValueError, "VERSION_BINDING"):
                journal.reserve(**(self.args | changed))

    def test_unknown_failure_keeps_slot(self):
        journal = self.journal(cap=1)
        key = journal.reserve(**self.args)
        journal.failed(key)
        self.assertEqual(journal.used, 1)
        self.assertIsNone(journal.reserve(**self.args))
        self.assertEqual(self.store.db.execute("SELECT state FROM analysis_calls").fetchone()[0], "UNKNOWN")

    def test_result_metrics_recorded_without_raw_evidence_or_usd_guess(self):
        journal = self.journal()
        key = journal.reserve(**(self.args | {"evidence": {"private_text": "canary-secret"}}))
        journal.finish(key, self.result | {"raw": "canary-secret"}, elapsed_seconds=0.12)
        raw = self.store.db.execute("SELECT result FROM analysis_calls").fetchone()[0]
        self.assertNotIn("canary-secret", raw)
        result = json.loads(raw)
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertIsNone(result["actual_usd"])
        self.assertFalse(result["confidence_calibrated"])

    def test_invalid_usage_rejected_and_no_result_overwrite(self):
        journal = self.journal()
        key = journal.reserve(**self.args)
        bad = self.result | {"usage": {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 1}}
        with self.assertRaises(ValueError):
            journal.finish(key, bad, elapsed_seconds=0.1)
        journal.finish(key, self.result, elapsed_seconds=0.1)
        with self.assertRaisesRegex(ValueError, "STATE_CONFLICT"):
            journal.finish(key, self.result, elapsed_seconds=0.1)

    def test_audit_keeps_valid_token_counts_but_redacts_secrets(self):
        self.store.emit("USAGE", usage=self.result["usage"], token="secret",
                        input_tokens="credential-canary", output_tokens=True)
        data = self.store.events()[-1]["payload"]
        self.assertEqual(data["usage"]["input_tokens"], 100)
        for key in ("token", "input_tokens", "output_tokens"):
            self.assertEqual(data[key], "[REDACTED]")

    async def test_bridge_recreation_cannot_reset_budget(self):
        runner = SimpleNamespace(model="test-model", select=AsyncMock(return_value=self.result))
        def bridge():
            return CodexFrameAnalysis(runner, lambda _: (self.signal,),
                AsyncMock(return_value={"evidence_eligible": True}), self.store, max_calls=1, clock=lambda: self.now)
        self.assertEqual((await bridge()(None)).direction, Direction.LONG)
        self.assertEqual((await bridge()(None)).direction, Direction.NO_TRADE)
        runner.select.assert_awaited_once()

    async def test_cancelled_bridge_preserves_unknown_budget(self):
        started = asyncio.Event()
        async def hang(*args):
            started.set()
            await asyncio.Event().wait()
        runner = SimpleNamespace(model="test-model", select=hang)
        bridge = CodexFrameAnalysis(runner, lambda _: (self.signal,), AsyncMock(return_value={"evidence_eligible": True}),
                                    self.store, max_calls=1, clock=lambda: self.now)
        task = asyncio.create_task(bridge(None))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(bridge.calls, 1)
        self.assertEqual(self.store.db.execute("SELECT state FROM analysis_calls").fetchone()[0], "UNKNOWN")

    def test_mixed_candidate_versions_rejected(self):
        journal = self.journal()
        with self.assertRaises(ValueError):
            journal.reserve(**(self.args | {"candidates": (self.signal, replace(self.signal, version="other", signal_id="other"))}))
