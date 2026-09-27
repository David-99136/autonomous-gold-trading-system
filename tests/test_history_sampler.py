"""GOLD 唯讀採樣器離線整合測試。

[Author: Antigravity | Date: 2026-09-27]
"""
import asyncio
import contextlib
import copy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from gold_system.history_sampler import capture
from gold_system.sampler_plan import Archive, SampleError, account_lease, diagnostic_lease, digest, read_plan, validate_plan
from gold_system.sampler_reader import DemoHistoryReader


class FakeClock:
    def __init__(self):
        self.base = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
        self.value = 100.0

    def mono(self):
        return self.value

    def wall(self):
        return self.base + timedelta(seconds=self.value-100)

    async def sleep(self, seconds):
        self.value += seconds


class SamplerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.plan = json.loads(Path("examples/history-sample-plan.json").read_text())
        self.requests = []
        self.hook = None
        self.prices = {"prices": [dict(snapshotTimeUTC=f"2026-01-05T09:{i}:00",
            openPrice=dict(bid=100, ask=101), highPrice=dict(bid=102, ask=103),
            lowPrice=dict(bid=99, ask=100), closePrice=dict(bid=101, ask=102)) for i in (57, 58, 59)]}

    def handler(self, request):
        self.requests.append(request)
        if self.hook:
            response = self.hook(request)
            if response is not None:
                return response
        path = request.url.path
        if path.endswith("/session"):
            return httpx.Response(200, json={"accountSecret": "secret-canary"},
                                  headers={"CST": "secret-cst", "X-SECURITY-TOKEN": "secret-token"})
        if path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": int(self.clock.wall().timestamp()*1000)})
        if path.endswith("/markets/GOLD"):
            return httpx.Response(200, json={"instrument": {"epic": "GOLD"}, "snapshot": {"marketStatus": "TRADEABLE"}})
        if path.endswith("/prices/GOLD"):
            return httpx.Response(200, json=self.prices)
        raise AssertionError("Unexpected endpoint")

    def reader(self):
        return DemoHistoryReader(dict(api_key="secret-canary", identifier="private-id", password="private-password"),
                                 transport=httpx.MockTransport(self.handler))

    async def run_capture(self):
        self.archive = Archive(self.root, "capture")
        with patch("socket.socket.connect", side_effect=AssertionError("NETWORK_FORBIDDEN")):
            return await capture(self.plan, self.reader(), self.archive, self.clock)

    def receipts(self):
        return [json.loads(p.read_text()) for p in sorted(self.archive.path.glob("request-*-complete.json"))]

    async def test_success_timing_hashes_no_secrets_or_permission(self):
        self.prices["password"] = "secret-canary"
        result = await self.run_capture()
        self.assertEqual(result["status"], "COMPLETED_UNVERIFIED")
        self.assertEqual(result["requests"], 10)  # login + two clock triplets + market + two prices
        self.assertTrue(result["completion_recorded"])
        for flag in ("entries_enabled", "closure_verified", "qualified"):
            self.assertFalse(result[flag])
        receipts = self.receipts()
        sends = [r["send_start"]["monotonic"] for r in receipts]
        self.assertTrue(all(b-a >= 1 for a, b in zip(sends, sends[1:])))
        for r in receipts:
            times = [r[k]["monotonic"] for k in ("queue_start", "send_start", "headers_received", "body_complete", "validation_complete")]
            self.assertEqual(times, sorted(times))
        prices = [r for r in receipts if r["kind"] == "PRICES"]
        self.assertTrue(all(r["clock_estimate"]["sample_request_ids"] for r in prices))
        raw = httpx.Response(200, json=self.prices).content
        self.assertEqual(prices[0]["body_sha256"], sha256(raw).hexdigest())
        file = self.archive.path / prices[0]["data_file"]
        self.assertEqual(prices[0]["data_sha256"], sha256(file.read_bytes()).hexdigest())
        self.assertNotEqual(prices[0]["body_sha256"], prices[0]["data_sha256"])
        all_text = "".join(p.read_text() for p in self.archive.path.glob("*.json"))
        for secret in ("secret-canary", "secret-cst", "secret-token", "private-id", "private-password"):
            self.assertNotIn(secret, all_text)
        self.assertEqual({r.url.host for r in self.requests}, {"demo-api-capital.backend-capital.com"})
        self.assertEqual([(r.method, r.url.path) for r in self.requests if r.method != "GET"], [("POST", "/api/v1/session")])

    async def test_http_error_and_redirect_stop_without_body_or_retry(self):
        for status in (401, 429, 500, 302):
            with self.subTest(status=status):
                self.clock = FakeClock()
                self.requests.clear()
                self.hook = lambda request: httpx.Response(status, text="secret-canary", headers={"Location": "https://example.invalid"})
                archive = Archive(self.root, f"status-{status}")
                result = await capture(self.plan, self.reader(), archive, self.clock)
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["requests"], 1)
                self.assertEqual(len(self.requests), 1)
                failed = json.loads((archive.path / "request-0001-failed.json").read_text())
                self.assertEqual(failed["http_status"], status)
                self.assertIsNone(failed["body_complete"])
                self.assertNotIn("secret-canary", json.dumps(failed))

    async def test_transport_timeout_not_retried(self):
        def timeout(request):
            raise httpx.ReadTimeout("secret-canary", request=request)
        self.hook = timeout
        result = await self.run_capture()
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["reason"], "READ_TRANSPORT_FAILED")

    async def test_cancelled_request_stops(self):
        def cancel(request):
            raise asyncio.CancelledError()
        self.hook = cancel
        result = await self.run_capture()
        self.assertEqual(result["reason"], "CANCELLED")
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_clock_offset_stops_before_market(self):
        self.hook = lambda r: httpx.Response(200, json={"serverTime": 0}) if r.url.path.endswith("/time") else None
        result = await self.run_capture()
        self.assertEqual(result["reason"], "CLOCK_OFFSET_EXCEEDS_LIMIT")
        self.assertEqual(len(self.requests), 4)

    async def test_clock_jump_rejected(self):
        def jump(request):
            self.clock.base += timedelta(seconds=2)
        self.hook = jump
        result = await self.run_capture()
        self.assertEqual(result["reason"], "CLOCK_JUMP")

    async def test_closed_market_no_price_requests(self):
        self.hook = lambda r: httpx.Response(200, json={"instrument": {"epic": "GOLD"}, "snapshot": {"marketStatus": "CLOSED"}}) if r.url.path.endswith("/markets/GOLD") else None
        result = await self.run_capture()
        self.assertEqual(result["reason"], "MARKET_UNAVAILABLE")
        self.assertFalse(any(r.url.path.endswith("/prices/GOLD") for r in self.requests))

    async def test_crossed_prices_saved_then_stopped(self):
        self.prices["prices"][0]["closePrice"]["ask"] = 100
        result = await self.run_capture()
        self.assertEqual(result["reason"], "PRICE_QUALITY_FAULT")
        prices = [r for r in self.receipts() if r["kind"] == "PRICES"]
        self.assertEqual(len(prices), 1)
        safe = json.loads((self.archive.path / prices[0]["data_file"]).read_text())
        self.assertIn("HISTORY_BID_ASK_CROSSED", [f["code"] for f in safe["faults"]])

    async def test_revision_stops_and_keeps_versions(self):
        price_count = 0
        def revise(request):
            nonlocal price_count
            if request.url.path.endswith("/prices/GOLD"):
                price_count += 1
                if price_count == 2:
                    self.prices["prices"][0]["highPrice"]["ask"] = 104
        self.hook = revise
        result = await self.run_capture()
        self.assertEqual(result["reason"], "PRICE_QUALITY_FAULT")
        prices = [r for r in self.receipts() if r["kind"] == "PRICES"]
        self.assertEqual(len(prices), 2)
        self.assertNotEqual(prices[0]["data_sha256"], prices[1]["data_sha256"])

    async def test_invalid_price_value_never_saved(self):
        self.prices["prices"][0]["closePrice"]["ask"] = "secret-canary"
        result = await self.run_capture()
        self.assertEqual(result["reason"], "PRICE_QUALITY_FAULT")
        self.assertNotIn("secret-canary", "".join(p.read_text() for p in self.archive.path.glob("*.json")))

    async def test_body_limit(self):
        self.hook = lambda r: httpx.Response(200, content=b" "*1_048_577)
        result = await self.run_capture()
        self.assertEqual(result["reason"], "RESPONSE_SIZE_LIMIT")
        failed = json.loads((self.archive.path / "request-0001-failed.json").read_text())
        self.assertIsNone(failed["body_complete"])

    async def test_bad_json(self):
        self.hook = lambda r: httpx.Response(200, content=b'{"secret-canary":NaN}')
        result = await self.run_capture()
        self.assertEqual(result["reason"], "RESPONSE_JSON_INVALID")

    async def test_late_schedule_is_not_caught_up(self):
        original = self.clock.sleep
        async def oversleep(seconds):
            await original(seconds+2 if seconds > 2 else seconds)
        self.clock.sleep = oversleep
        result = await self.run_capture()
        self.assertEqual(result["reason"], "SCHEDULE_MISSED")
        self.assertFalse(any(r.url.path.endswith("/prices/GOLD") for r in self.requests))

    async def test_disk_failure_prevents_sending(self):
        self.archive = Archive(self.root, "capture")
        with patch.object(self.archive, "put", side_effect=OSError("secret-canary")):
            result = await capture(self.plan, self.reader(), self.archive, self.clock)
        self.assertFalse(self.requests)
        self.assertFalse(result["completion_recorded"])
        self.assertEqual(result["reason"], "ARCHIVE_WRITE_FAILED")

    async def test_missing_commit_marker_not_success(self):
        self.archive = Archive(self.root, "capture")
        put = self.archive.put
        def fail(name, value):
            if name == "request-0006-complete.json":
                raise OSError("disk")
            return put(name, value)
        with patch.object(self.archive, "put", fail):
            result = await capture(self.plan, self.reader(), self.archive, self.clock)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue((self.archive.path / "request-0006-data.json").exists())
        self.assertFalse((self.archive.path / "request-0006-complete.json").exists())
        self.assertEqual(result["requests"], 6)

    async def test_direct_reader_rejects_non_allowlist(self):
        reader = self.reader()
        try:
            for kind in ("POST /positions", "LIVE", "https://example.invalid"):
                with self.assertRaises(SampleError):
                    await reader.send(kind, {}, {}, lambda _: None)
            self.assertFalse(self.requests)
        finally:
            await reader.close()

    async def test_plan_hash_mismatch_does_not_read_vault(self):
        from gold_system.sampler_cli import run_demo
        with patch("gold_system.capital.load_credentials", side_effect=AssertionError("VAULT_FORBIDDEN")):
            with self.assertRaisesRegex(SampleError, "PLAN_HASH_MISMATCH"):
                await run_demo("examples/history-sample-plan.json", "0"*64, "capture", confirmed=True, exclusive_account=True)

    async def test_explicit_confirmation_required(self):
        from gold_system.sampler_cli import run_demo
        with self.assertRaisesRegex(SampleError, "EXPLICIT_DEMO"):
            await run_demo("missing", "", "capture", confirmed=False, exclusive_account=False)

    async def test_request_budget_counts_session_and_clock(self):
        with patch("gold_system.history_sampler.MAX_REQUESTS", 3):
            result = await self.run_capture()
        self.assertEqual(result["reason"], "REQUEST_LIMIT")
        self.assertEqual(result["requests"], 3)
        self.assertEqual(len(self.requests), 3)

    async def test_per_request_deadline_stops(self):
        def slow(request):
            self.clock.value += 11
        self.hook = slow
        result = await self.run_capture()
        self.assertEqual(result["reason"], "REQUEST_DEADLINE")
        self.assertEqual(len(self.requests), 1)

    async def test_run_deadline_stops_during_wait(self):
        original = self.clock.sleep
        async def oversleep(seconds):
            await original(seconds+200 if seconds > 2 else seconds)
        self.clock.sleep = oversleep
        result = await self.run_capture()
        self.assertEqual(result["reason"], "RUN_DEADLINE")
        self.assertFalse(any(r.url.path.endswith("/prices/GOLD") for r in self.requests))

    async def test_relogin_rejected(self):
        reader = self.reader()
        receipt = {}
        try:
            await reader.send("SESSION", {}, receipt, lambda _: None)
            with self.assertRaisesRegex(SampleError, "RELOGIN_FORBIDDEN"):
                await reader.send("SESSION", {}, {}, lambda _: None)
            self.assertEqual(len(self.requests), 1)
        finally:
            await reader.close()

    async def test_reader_query_injection_rejected(self):
        reader = self.reader()
        try:
            await reader.send("SESSION", {}, {}, lambda _: None)
            query = {k: self.plan["queries"][0][k] for k in ("from", "to", "resolution", "max")}
            with self.assertRaises(SampleError):
                await reader.send("PRICES", query | {"url": "https://example.invalid"}, {}, lambda _: None)
            self.assertEqual(len(self.requests), 1)
        finally:
            await reader.close()

    def test_account_lease_across_instances_and_identifier_case(self):
        with patch("gold_system.sampler_plan.tempfile.gettempdir", return_value=str(self.root)):
            with account_lease("Synthetic-User"):
                with self.assertRaisesRegex(SampleError, "LEASE_BUSY"):
                    with account_lease("synthetic-user"):
                        pass
            with account_lease("synthetic-user"):
                pass

    def test_stale_lease_never_removed_by_failed_acquirer(self):
        lease = self.root / "history-sampler.lease"
        lease.write_bytes(b"stale-test-owner")
        with self.assertRaises(SampleError):
            with diagnostic_lease(self.root):
                pass
        self.assertEqual(lease.read_bytes(), b"stale-test-owner")

    def test_plan_duplicate_keys_size_and_naive_times(self):
        file = self.root / "bad.json"
        for raw in (b'{"version":1,"version":1}', b" "*(128*1024+1), b"secret-canary"):
            file.write_bytes(raw)
            with self.assertRaises(SampleError):
                read_plan(file)
        with self.assertRaises(SampleError):
            validate_plan(self.plan | {"start": "2026-01-05T10:00:00"})

    async def test_missing_body_receipt_is_not_success(self):
        class BrokenReader:
            async def send(self, kind, query, receipt, mark):
                return {}
            async def close(self):
                pass
        archive = Archive(self.root, "capture")
        result = await capture(self.plan, BrokenReader(), archive, self.clock)
        self.assertEqual(result["reason"], "RECEIPT_INCOMPLETE")

    def test_plan_limits_and_extra_fields(self):
        changes = [dict(epic="ETHUSD"), dict(environment="LIVE"), dict(version=True),
                   dict(url="https://example.invalid"), dict(end="2026-01-05T12:00:00Z"), dict(queries=[])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(SampleError):
                validate_plan(self.plan | change)
        for change in (dict(max=True), dict(resolution="DAY"), {"from": "2026-01-05T09:57:01Z"},
                       {"to": "2026-01-06T00:00:00Z"}, dict(at="2026-01-05T10:00:01Z"), dict(headers={} )):
            plan = copy.deepcopy(self.plan)
            plan["queries"][0].update(change)
            with self.subTest(change=change), self.assertRaises(SampleError):
                validate_plan(plan)

    def test_plan_hash_is_normalized(self):
        normalized = validate_plan(self.plan)
        self.assertEqual(digest(normalized), digest(validate_plan(normalized)))

    def test_exclusive_archive_and_paths(self):
        Archive(self.root, "capture")
        with self.assertRaises(FileExistsError):
            Archive(self.root, "capture")
        for name in ("../escape", ".", "a/b", "C:/test"):
            with self.assertRaises(SampleError):
                Archive(self.root, name)

    def test_archive_size_and_no_overwrite(self):
        archive = Archive(self.root, "capture")
        archive.put("file.json", {})
        with self.assertRaises(FileExistsError):
            archive.put("file.json", {})
        archive.size = 64*1024*1024
        with self.assertRaisesRegex(SampleError, "ARCHIVE_SIZE_LIMIT"):
            archive.put("other.json", {})

    def test_lease_excludes_parallel_and_releases_owned_file(self):
        with diagnostic_lease(self.root):
            with self.assertRaisesRegex(SampleError, "LEASE_BUSY"):
                with diagnostic_lease(self.root):
                    pass
        self.assertFalse((self.root / "history-sampler.lease").exists())

    def test_offline_plan_cli(self):
        import sys
        from gold_system.cli import main
        out = io.StringIO()
        with patch.object(sys, "argv", ["gold_system", "history-sample-plan", "--plan", "examples/history-sample-plan.json"]), \
                patch("gold_system.capital.load_credentials", side_effect=AssertionError("VAULT_FORBIDDEN")), \
                patch("socket.socket.connect", side_effect=AssertionError("NETWORK_FORBIDDEN")), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as exc:
                main()
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "PLAN_VALID")
