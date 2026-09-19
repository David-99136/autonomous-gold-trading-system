import json
import unittest
import tempfile
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gold_system.codex_analysis import parse_events, CodexFrameAnalysis, CodexAnalysis
from gold_system.core import D, Direction, Mode, RiskPolicy, Signal
from gold_system.store import Store


class CodexDecisionTests(unittest.TestCase):
    def events(self, selection="NO_TRADE", confidence=0, extra=None):
        data = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
            "candidate_id": selection, "confidence": confidence, "reason_code": "INSUFFICIENT_EVIDENCE"})}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 10}}]
        if extra:
            data.insert(0, extra)
        return "\n".join(json.dumps(x) for x in data)

    def test_usage_is_not_fabricated_as_free_usd_cost(self):
        result = parse_events(self.events(), ["NO_TRADE"])
        self.assertIsNone(result["actual_usd"])
        self.assertEqual(result["usage"]["output_tokens"], 10)
        self.assertFalse(result["confidence_calibrated"])

    def test_unknown_candidate_nonfinite_confidence_and_tool_activity_rejected(self):
        for raw in (self.events("unapproved"), self.events(confidence=float("nan")),
                    self.events(extra={"type": "item.completed", "item": {"type": "command_execution"}}),
                    self.events(extra={"type": "turn.failed"})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_events(raw, ["NO_TRADE"])

    def test_partial_output_without_completion_is_not_a_decision(self):
        with self.assertRaises(ValueError):
            parse_events(self.events().splitlines()[0], ["NO_TRADE"])


class CodexBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"audit.db")
        self.now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
        self.signal = Signal("candidate1", RiskPolicy().version, self.now, self.now+timedelta(seconds=60),
                             Direction.SHORT, Mode.LEFT, D(2010), D(1970), "STRUCTURAL_CONFIRMATION")
        self.runner = SimpleNamespace(model="fixture", select=AsyncMock(return_value={"candidate_id": "candidate1",
            "confidence": 0.8, "reason_code": "EVIDENCE_SUPPORTS", "actual_usd": None,
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}}))
        self.evidence = AsyncMock(return_value={"evidence_eligible": True, "records": {}})
        self.bridge = CodexFrameAnalysis(self.runner, lambda _: (self.signal,), self.evidence,
                                        self.store, max_calls=1, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_selected_candidate_preserves_structural_prices_and_call_cap(self):
        result = await self.bridge(None)
        self.assertIs(result, self.signal)
        self.assertEqual((await self.bridge(None)).direction, Direction.NO_TRADE)
        self.runner.select.assert_awaited_once()
        self.assertIsNone(self.store.events()[-1]["payload"]["actual_usd"])

    async def test_missing_evidence_and_operator_stop_do_not_call_codex(self):
        self.evidence.return_value = {"evidence_eligible": False}
        self.assertEqual((await self.bridge(None)).direction, Direction.NO_TRADE)
        self.evidence.return_value = {"evidence_eligible": True}
        self.store.stop_new_entries()
        self.assertEqual((await self.bridge(None)).direction, Direction.NO_TRADE)
        self.runner.select.assert_not_awaited()

    async def test_expired_model_result_does_not_enter(self):
        result = self.runner.select.return_value
        async def late(*args):
            self.now += timedelta(seconds=61)
            return result
        self.runner.select.side_effect = late
        self.assertEqual((await self.bridge(None)).direction, Direction.NO_TRADE)

    async def test_failed_attempt_consumes_limit_and_is_not_retried(self):
        self.runner.select.side_effect = RuntimeError("MODEL_OFFLINE")
        with self.assertRaises(RuntimeError):
            await self.bridge(None)
        self.assertEqual((await self.bridge(None)).direction, Direction.NO_TRADE)
        self.runner.select.assert_awaited_once()


class CodexProcessTests(unittest.IsolatedAsyncioTestCase):
    def process(self):
        return SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
            stdout=SimpleNamespace(read=AsyncMock(side_effect=[CodexDecisionTests().events().encode(), b""])),
            wait=AsyncMock(), kill=Mock(), returncode=0)

    async def test_native_process_receives_only_input_and_no_broker_key(self):
        process = self.process()
        with patch.dict(os.environ, {"CAPITAL_API_KEY": "fixture-private-key"}), \
                patch("gold_system.codex_analysis.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await CodexAnalysis(executable=sys.executable).select([], {"evidence_eligible": False})
        self.assertEqual(result["candidate_id"], "NO_TRADE")
        self.assertIsNone(result["requested_model"])
        self.assertNotIn("CAPITAL_API_KEY", spawn.call_args.kwargs["env"])
        args = spawn.call_args.args
        self.assertIn("--ignore-user-config", args)
        self.assertIn("read-only", args)
        self.assertIn("shell_tool", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn(b"fixture-private-key", process.stdin.write.call_args.args[0])

    async def test_timeout_kills_and_reaps_process(self):
        process = self.process()
        process.returncode = None
        async def hang(*args):
            await asyncio.Event().wait()
        process.stdout.read = hang
        with patch("gold_system.codex_analysis.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(RuntimeError, "UNAVAILABLE"):
                await CodexAnalysis(executable=sys.executable, timeout=0.01).select([], {"evidence_eligible": False})
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
