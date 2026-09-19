import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gold_system.broker import PaperBroker
from gold_system.core import D, Contract, Direction, Mode, Quality, Quote, RiskPolicy, Signal
from gold_system.engine import Engine
from gold_system.service import MarketFrame, PaperService
from gold_system.store import Store
from gold_system.codex_analysis import CodexFrameAnalysis


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"service.db")
        self.now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
        self.policy = RiskPolicy()
        self.contract = Contract("SYNTHETIC", D(1), D("0.01"), D("0.01"), D(100), D("0.05"), D("0.1"))
        self.engine = Engine(self.store, PaperBroker(), self.contract, self.policy)
        self.quality = Quality(True, True, True, False, D(1), 0, D(120), D(10), False, True, True)
        self.signal = Signal("signal1", self.policy.version, self.now, self.now+timedelta(minutes=1),
                             Direction.LONG, Mode.RIGHT, D(1990), D(2030), "fixture")

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def frame(self, bid=D(2000)):
        return MarketFrame(Quote(self.now, bid, bid+D("0.5")), self.quality)

    async def audit_wait(self, kind):
        for _ in range(100):
            if any(e["kind"] == kind for e in self.store.events()):
                return
            await asyncio.sleep(0)
        self.fail(f"Missing expected event: {kind}")

    async def test_signal_reaches_engine_without_blocking_feed(self):
        async def analyze(frame):
            return self.signal
        service = PaperService(self.engine, analyze, clock=lambda: self.now)
        async def feed():
            yield self.frame()
            await self.audit_wait("ANALYSIS_READY")
            self.now += timedelta(milliseconds=100)
            yield self.frame()
        await service.run(feed())
        self.assertIsNotNone(self.engine.broker.position)
        self.assertEqual(sum(e["kind"] == "ENTRY" for e in self.store.events()), 1)
        self.assertTrue(self.store.events()[-1]["payload"]["position_open"])
        with self.assertRaisesRegex(RuntimeError, "single-use"):
            await service.run(feed())

    async def test_codex_bridge_to_trading_engine_with_mock_model(self):
        runner = SimpleNamespace(model="fixture", select=AsyncMock(return_value={
            "candidate_id": self.signal.signal_id, "actual_usd": None, "confidence": 0.8,
            "reason_code": "EVIDENCE_SUPPORTS", "usage": {"input_tokens": 20, "output_tokens": 10}}))
        bridge = CodexFrameAnalysis(runner, lambda _: (self.signal,),
            AsyncMock(return_value={"evidence_eligible": True}), self.store, max_calls=1, clock=lambda: self.now)
        service = PaperService(self.engine, bridge, analysis_timeout=46, clock=lambda: self.now)
        async def feed():
            yield self.frame()
            await self.audit_wait("ANALYSIS_READY")
            self.now += timedelta(milliseconds=100)
            yield self.frame()
        await service.run(feed())
        runner.select.assert_awaited_once()
        self.assertIsNotNone(self.engine.broker.position)
        self.assertTrue(any(e["kind"] == "CODEX_ANALYSIS_COMPLETED" for e in self.store.events()))

    async def test_hung_analysis_does_not_delay_existing_stop_and_is_cancelled(self):
        await self.engine.tick(self.now, self.frame().quote, self.quality, self.signal)
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def analyze(frame):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        service = PaperService(self.engine, analyze, clock=lambda: self.now)
        async def feed():
            self.now += timedelta(milliseconds=100)
            yield self.frame()
            await started.wait()
            self.now += timedelta(milliseconds=100)
            yield self.frame(D(1989))
        await asyncio.wait_for(service.run(feed()), 1)
        self.assertIsNone(self.engine.broker.position)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(any(e["kind"] == "EXIT" and e["payload"]["reason"] == "STOP" for e in self.store.events()))

    async def test_provider_failure_never_leaks_private_error_or_enters(self):
        async def analyze(frame):
            raise RuntimeError("private-token-canary")
        service = PaperService(self.engine, analyze, clock=lambda: self.now)
        async def feed():
            yield self.frame()
            await self.audit_wait("ANALYSIS_UNAVAILABLE")
            self.now += timedelta(milliseconds=100)
            yield self.frame()
        await service.run(feed())
        self.assertIsNone(self.engine.broker.position)
        self.assertNotIn("private-token-canary", str(self.store.events()))

    async def test_late_result_after_feed_fault_cannot_reenable_entry(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def analyze(frame):
            started.set()
            await release.wait()
            return self.signal
        service = PaperService(self.engine, analyze, clock=lambda: self.now)
        async def feed():
            frame = self.frame()
            yield frame
            await started.wait()
            yield frame  # 重複報價使故障前的分析工作失效。
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            self.now += timedelta(milliseconds=100)
            yield self.frame()
        await service.run(feed())
        self.assertIsNone(self.engine.broker.position)

    async def test_cancellation_reclaims_analysis_task(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def analyze(frame):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        service = PaperService(self.engine, analyze, clock=lambda: self.now)
        async def feed():
            yield self.frame()
            await asyncio.Event().wait()
        task = asyncio.create_task(service.run(feed()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_silent_feed_locks_entries_and_cancels_analysis(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def analyze(frame):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def feed():
            yield self.frame()
            await started.wait()
            await asyncio.Event().wait()
        service = PaperService(self.engine, analyze, feed_timeout=.05, clock=lambda: self.now)
        with self.assertRaisesRegex(RuntimeError, "PAPER_SERVICE_FAILED"):
            await service.run(feed())
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.engine.broker.position)
        self.assertTrue(any(e["payload"].get("reason") == "FEED_SILENT" for e in self.store.events()))
