"""ETH 恢復入口的離線 HTTP／持久化驗證。

[Author: Codex | Date: 2026-09-27]
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from gold_system.close_eth_demo import close
from gold_system.store import Store


class RecoveryCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "audit.db"
        Store(self.database).close()
        self.output = self.root / "close.json"
        self.mode = "still_open"
        self.deletes = 0
        self.positions_reads = 0
        self.deal = {"position": {"dealId": "owned"}, "market": {"epic": "ETHUSD"}}

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def handle(self, request):
        path = request.url.path
        if path.endswith("/session"):
            account = "different" if self.mode == "account_changed" and self.deletes else "fixture-account"
            return httpx.Response(200, json={"accountId": account, "currency": "USD"},
                                  headers={"CST": "fixture", "X-SECURITY-TOKEN": "fixture"})
        if request.method == "DELETE":
            self.deletes += 1
            # 在 HTTP 邊界檢查：副作用前就已寫入可重啟讀取的意圖與新倉鎖。
            checkpoint = Store(self.database, must_exist=True)
            try:
                self.assertTrue(checkpoint.entries_stopped())
                self.assertTrue(checkpoint.unresolved_intents())
                self.assertTrue(any(e["kind"] == "ETH_RECOVERY_INTENT" for e in checkpoint.events()))
            finally:
                checkpoint.close()
            self.assertEqual(json.loads(self.output.read_text())["outcome"], "RECOVERY_LOCKED")
            if self.mode == "timeout":
                raise httpx.ReadTimeout("private-response-canary")
            if self.mode == "cancel":
                raise asyncio.CancelledError()
            return httpx.Response(200, json={"dealReference": "close-ref"})
        if path.endswith("/confirms/close-ref"):
            if self.mode == "confirm_failure":
                raise httpx.ReadTimeout("private-response-canary")
            reference = "other-ref" if self.mode == "wrong_reference" else "close-ref"
            return httpx.Response(200, json={"dealStatus": "ACCEPTED", "dealReference": reference})
        if path.endswith("/positions"):
            self.positions_reads += 1
            if self.positions_reads > 1 and self.mode == "malformed":
                return httpx.Response(200, json={"positions": None})
            if self.deletes and self.mode in ("closed", "account_changed", "wrong_reference", "new_order"):
                return httpx.Response(200, json={"positions": []})
            return httpx.Response(200, json={"positions": [self.deal]})
        if path.endswith("/workingorders"):
            if self.deletes and self.mode == "new_order":
                return httpx.Response(200, json={"workingOrders": [{"workingOrderData": {"dealId": "new"}}]})
            return httpx.Response(200, json={"workingOrders": []})
        raise AssertionError("Unexpected endpoint")

    async def run_close(self, output=None):
        with patch("gold_system.close_eth_demo.load_credentials", return_value={
                "api_key": "fixture", "identifier": "fixture", "password": "fixture"}), \
                patch("socket.socket.connect", side_effect=AssertionError("NETWORK_FORBIDDEN")):
            return await close("owned", output or self.output, database=self.database,
                               transport=httpx.MockTransport(self.handle))

    async def test_accepted_close_with_remaining_owned_position_stays_locked(self):
        result = await self.run_close()
        self.assertEqual(result["outcome"], "RECOVERY_LOCKED")
        self.assertEqual(result["position_count"], 1)
        store = Store(self.database, must_exist=True)
        try:
            self.assertTrue(store.entries_stopped())
            self.assertTrue(store.unresolved_intents())
        finally:
            store.close()

    async def test_confirmed_absence_closes_but_never_unlocks_entries(self):
        self.mode = "closed"
        result = await self.run_close()
        self.assertEqual(result["outcome"], "CLOSED")
        self.assertEqual(result["position_count"], 0)
        self.assertEqual(json.loads(self.output.read_text()), result)
        store = Store(self.database, must_exist=True)
        try:
            self.assertFalse(store.unresolved_intents())
            self.assertTrue(store.entries_stopped())
        finally:
            store.close()

    async def test_timeout_leaves_durable_unknown_and_different_output_cannot_resubmit(self):
        self.mode = "timeout"
        self.assertEqual((await self.run_close())["outcome"], "RECOVERY_LOCKED")
        with self.assertRaisesRegex(ValueError, "RECOVERY_ALREADY_RECORDED"):
            await self.run_close(self.root / "another.json")
        self.assertEqual(self.deletes, 1)
        store = Store(self.database, must_exist=True)
        try:
            self.assertTrue(store.unresolved_intents())
            self.assertNotIn("private-response-canary", str(store.events()))
        finally:
            store.close()

    async def test_reference_survives_failed_confirmation(self):
        self.mode = "confirm_failure"
        self.assertEqual((await self.run_close())["outcome"], "RECOVERY_LOCKED")
        store = Store(self.database, must_exist=True)
        try:
            refs = [e["payload"]["reference"] for e in store.events() if e["kind"] == "ETH_RECOVERY_REFERENCE"]
            self.assertEqual(refs, ["close-ref"])
            self.assertTrue(store.unresolved_intents())
        finally:
            store.close()

    async def test_cancellation_keeps_recovery_state(self):
        self.mode = "cancel"
        with self.assertRaises(asyncio.CancelledError):
            await self.run_close()
        store = Store(self.database, must_exist=True)
        try:
            self.assertTrue(store.unresolved_intents())
            self.assertTrue(store.entries_stopped())
        finally:
            store.close()

    async def test_invalid_final_snapshot_never_means_flat(self):
        self.mode = "malformed"
        self.assertEqual((await self.run_close())["outcome"], "RECOVERY_LOCKED")

    async def test_account_switch_never_reports_closed(self):
        self.mode = "account_changed"
        self.assertEqual((await self.run_close())["outcome"], "RECOVERY_LOCKED")

    async def test_unrelated_confirmation_never_reports_closed(self):
        self.mode = "wrong_reference"
        self.assertEqual((await self.run_close())["outcome"], "RECOVERY_LOCKED")

    async def test_new_working_order_requires_recovery_even_if_position_absent(self):
        self.mode = "new_order"
        result = await self.run_close()
        self.assertEqual(result["outcome"], "RECOVERY_LOCKED")
        self.assertEqual(result["working_order_count"], 1)

    async def test_missing_audit_database_is_not_silently_created(self):
        self.database = self.root / "missing.db"
        with self.assertRaises(Exception):
            await self.run_close()
        self.assertFalse(self.database.exists())
        self.assertEqual(self.deletes, 0)

    async def test_existing_output_is_not_overwritten_or_submitted(self):
        self.output.write_text("existing-evidence", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "RECOVERY_OUTPUT_EXISTS"):
            await self.run_close()
        self.assertEqual(self.output.read_text(), "existing-evidence")
        self.assertEqual(self.deletes, 0)

    async def test_durable_intent_failure_prevents_broker_delete(self):
        # 模擬檔案系統無法持久化；不能冒險繼續送出。
        with patch("gold_system.close_eth_demo.os.fsync", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await self.run_close()
        self.assertEqual(self.deletes, 0)
