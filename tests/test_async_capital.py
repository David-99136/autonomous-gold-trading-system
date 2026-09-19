import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from gold_system.async_capital import AsyncCapitalDemo, DEMO_BASE, encode_flat
from gold_system.core import D, Direction, Mode, Signal, Plan, Rejected
from gold_system.order_journal import OrderJournal, dispatch_once
from gold_system.store import Store


class AsyncCapitalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"audit.db")
        self.journal = OrderJournal(self.store)
        now = datetime.now(timezone.utc)
        signal = Signal("order1", "test", now, now+timedelta(minutes=1), Direction.LONG,
                        Mode.RIGHT, D("1990.01"), D("2030.12"), "test")
        self.journal.prepare(Plan(signal, D(2000), D("0.12"), D(2), D(10)), "GOLD", sha256(b"account").hexdigest())
        self.requests = []
        self.response_mode = "normal"
        async def handle(request):
            self.requests.append(request)
            if self.response_mode == "timeout":
                raise httpx.ReadTimeout("private api key details", request=request)
            if self.response_mode == "redirect":
                return httpx.Response(302, headers={"location": "https://example.com/steal"})
            if self.response_mode == "error":
                return httpx.Response(401, json={"password": "private-value"})
            if request.url.path == "/api/v1/session" and request.method == "POST":
                return httpx.Response(200, json={"private": "ignored"}, headers={"CST": "session-cst", "X-SECURITY-TOKEN": "session-security"})
            if request.url.path == "/api/v1/session":
                return httpx.Response(200, json={"accountId": "other" if self.response_mode == "switched" else "account", "currency": "USD"})
            if request.url.path == "/api/v1/positions" and request.method == "POST":
                if self.response_mode == "post_timeout":
                    raise httpx.ReadTimeout("private order transport data", request=request)
                return httpx.Response(200, json={"dealReference": "o_ref"})
            return httpx.Response(200, json={})
        self.client = AsyncCapitalDemo(transport=httpx.MockTransport(handle))
        await self.client.login({"identifier": "private-login", "api_key": "private-key", "password": "private-password"})

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_session_credentials_are_not_reused_as_api_key(self):
        await self.client.session()
        request = self.requests[-1]
        self.assertEqual(str(request.url), DEMO_BASE+"/session")
        self.assertNotIn("X-CAP-API-KEY", request.headers)
        self.assertEqual(request.headers["CST"], "session-cst")
        self.assertNotIn("private-password", str(self.client._headers))

    async def test_write_is_disabled_by_default_even_with_journal(self):
        self.journal.transition("order1", "SUBMITTING")
        with self.assertRaisesRegex(Rejected, "DEMO_WRITES_DISABLED"):
            await self.client.post_prepared_open(self.journal, "order1")
        self.assertEqual(len(self.requests), 1)  # 只有假登入，沒有訂單。

    async def test_journal_to_http_mock_writes_exact_decimal_and_stop(self):
        self.client._write_guard = AsyncMock(return_value=True)  # 僅 MockTransport 測試。
        async def send():
            return await self.client.post_prepared_open(self.journal, "order1")
        result = await dispatch_once(self.journal, "order1", send, AsyncMock(return_value={"outcome": "CONFIRMED"}))
        self.assertEqual(result["state"], "CONFIRMED")
        request = self.requests[-1]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), DEMO_BASE+"/positions")
        payload = json.loads(request.content, parse_float=D)
        self.assertEqual(payload["size"], D("0.12"))
        self.assertEqual(payload["stopLevel"], D("1990.01"))
        self.assertNotIn(b'"0.12"', request.content)
        self.assertFalse(payload["trailingStop"])
        self.assertEqual(set(payload), {"epic", "direction", "size", "stopLevel", "profitLevel", "guaranteedStop", "trailingStop"})

    async def test_guard_rejection_prevents_all_order_network_calls(self):
        self.client._write_guard = AsyncMock(return_value=False)
        self.journal.transition("order1", "SUBMITTING")
        with self.assertRaisesRegex(Rejected, "GUARD_REJECTED"):
            await self.client.post_prepared_open(self.journal, "order1")
        self.assertEqual(len(self.requests), 1)

    async def test_redirect_does_not_forward_credentials(self):
        self.response_mode = "redirect"
        with self.assertRaisesRegex(RuntimeError, "DEMO_HTTP_302"):
            await self.client.session()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(r.url.host == "demo-api-capital.backend-capital.com" for r in self.requests))

    async def test_timeout_not_retried_or_exposed(self):
        self.response_mode = "timeout"
        with self.assertRaises(RuntimeError) as caught:
            await self.client.session()
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(self.requests), 2)

    async def test_http_body_not_exposed(self):
        self.response_mode = "error"
        with self.assertRaises(RuntimeError) as caught:
            await self.client.session()
        self.assertNotIn("private-value", str(caught.exception))

    async def test_cancelled_login_clears_session(self):
        self.client._request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.client.login({"identifier": "a", "password": "b", "api_key": "c"})
        self.assertEqual(self.client._headers, {})

    async def test_arbitrary_domain_path_blocked(self):
        for path in ("https://example.com", "//example.com"):
            with self.assertRaises(Rejected):
                await self.client._request("GET", path)
        self.assertEqual(len(self.requests), 1)

    async def test_guard_cannot_change_state_and_then_send(self):
        async def guard(journal, intent):
            journal.transition(intent, "UNKNOWN")
            return True
        self.client._write_guard = guard
        self.journal.transition("order1", "SUBMITTING")
        with self.assertRaisesRegex(Rejected, "STATE_CHANGED"):
            await self.client.post_prepared_open(self.journal, "order1")
        self.assertFalse(any(r.method == "POST" and r.url.path.endswith("positions") for r in self.requests))

    async def test_account_switch_blocks_order_post(self):
        self.response_mode = "switched"
        self.client._write_guard = AsyncMock(return_value=True)
        self.journal.transition("order1", "SUBMITTING")
        with self.assertRaisesRegex(Rejected, "ACCOUNT_CHANGED"):
            await self.client.post_prepared_open(self.journal, "order1")
        self.assertFalse(any(r.url.path.endswith("positions") for r in self.requests))

    async def test_post_timeout_is_unknown_and_cannot_send_twice(self):
        self.response_mode = "post_timeout"
        self.client._write_guard = AsyncMock(return_value=True)
        async def send():
            return await self.client.post_prepared_open(self.journal, "order1")
        result = await dispatch_once(self.journal, "order1", send, AsyncMock(return_value={"outcome": "UNKNOWN"}))
        self.assertEqual(result["state"], "UNKNOWN")
        with self.assertRaisesRegex(Rejected, "ALREADY_SENT"):
            await send()
        self.assertEqual(sum(r.url.path.endswith("positions") for r in self.requests), 1)
        self.assertNotIn("private order", str(self.store.events()))

    def test_decimal_wire_values_are_exact_and_finite(self):
        self.assertEqual(encode_flat({"size": D("0.0100000001")}), b'{"size":0.0100000001}')
        for bad in (D("NaN"), D("Infinity"), D(-1), D("1e-100"), D("1e100")):
            with self.assertRaises(Rejected):
                encode_flat({"size": bad})
