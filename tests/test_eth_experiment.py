import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gold_system.clock_sync import BrokerClock, synchronize
from gold_system.eth_experiment import Experiment, check_market
from gold_system.streaming import FeedError, parse_quote
from tests.test_streaming import NOW, tick
from gold_system.core import Quote, D


class EthTests(unittest.IsolatedAsyncioTestCase):
    async def test_net_reduction_empty_affected_deals_uses_snapshot_and_cleans(self):
        client = AsyncMock()
        client.session.return_value = {"accountId": "fixture", "currency": "USD"}
        client.preferences.return_value = {"hedgingMode": False}
        client.working_orders.return_value = {"workingOrders": []}
        def position(size, stop):
            return {"position": {"dealId": "owned", "direction": "BUY", "size": size,
                    "stopLevel": stop, "guaranteedStop": True}, "market": {"epic": "ETHUSD"}}
        client.positions.side_effect = [{"positions": p} for p in ([], [position(".002", 2600)],
            [position(".002", 2601)], [position(".001", 2601)], [position(".001", 2601)], [])]
        market = {"instrument": {"epic": "ETHUSD", "currency": "USD", "type": "CRYPTOCURRENCIES",
                  "guaranteedStopAllowed": True}, "snapshot": {"marketStatus": "TRADEABLE", "marketModes": ["REGULAR"]},
                  "dealingRules": {"minDealSize": {"unit": "POINTS", "value": ".001"},
                    "minSizeIncrement": {"unit": "POINTS", "value": ".001"},
                    "minGuaranteedStopDistance": {"unit": "PERCENTAGE", "value": ".5"}}}
        async def request(method, path, payload=None):
            return (market if path == "/markets/ETHUSD" else {"dealReference": "fixture"}), {}
        client._request.side_effect = request
        client.confirmation.side_effect = [
            {"dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "owned", "status": "OPENED"}]},
            {"dealStatus": "ACCEPTED"}, {"dealStatus": "ACCEPTED", "affectedDeals": []},
            {"dealStatus": "ACCEPTED", "status": "CLOSED"}]
        async def quotes(**kwargs):
            yield Quote(NOW, D(2620), D(2622))
        client.quotes = quotes
        clock = SimpleNamespace(now=lambda: NOW, offset_ms=0, uncertainty_ms=100)
        with tempfile.TemporaryDirectory() as root:
            with (Path(root)/"audit.jsonl").open("w", encoding="utf-8") as audit:
                with patch("gold_system.eth_experiment.synchronize", new=AsyncMock(return_value=clock)):
                    result = await Experiment(client, audit).run()
        self.assertEqual(result, {"outcome": "LIFECYCLE_VERIFIED", "clean": True})
        self.assertEqual(sum(c.args[0] == "DELETE" for c in client._request.await_args_list), 1)

    def test_eth_requires_explicit_symbol(self):
        with self.assertRaises(FeedError):
            parse_quote(tick(epic="ETHUSD"), NOW)
        self.assertTrue(parse_quote(tick(epic="ETHUSD"), NOW, epic="ETHUSD").valid())

    def test_clock_corrects_future_local_timestamp_without_changing_quote(self):
        raw = tick(epic="ETHUSD")
        local = NOW-timedelta(milliseconds=400)
        with self.assertRaises(FeedError):
            parse_quote(raw, local, epic="ETHUSD")
        clock = BrokerClock(local+timedelta(milliseconds=600), time.monotonic(), 600, 150)
        value = parse_quote(raw, clock.now(), epic="ETHUSD")
        self.assertEqual(value.timestamp, NOW)

    def test_clock_expires(self):
        clock = BrokerClock(NOW, time.monotonic()-61, 500, 100)
        with self.assertRaisesRegex(RuntimeError, "CLOCK_ESTIMATE_EXPIRED"):
            clock.now()

    async def test_clock_over_limit_rejects(self):
        client = AsyncMock()
        client._request.return_value = ({"serverTime": 102000}, {})
        with patch("gold_system.clock_sync.time.time", return_value=100), \
             patch("gold_system.clock_sync.time.monotonic", return_value=10):
            with self.assertRaisesRegex(RuntimeError, "CLOCK_OFFSET_EXCEEDS_LIMIT"):
                await synchronize(client)

    async def test_failed_write_never_retried(self):
        client = AsyncMock()
        client.session.return_value = {"accountId": "fixture", "currency": "USD"}
        client._request.side_effect = TimeoutError("private-token")
        with tempfile.TemporaryDirectory() as root:
            with (Path(root)/"audit.jsonl").open("w+", encoding="utf-8") as audit:
                experiment = Experiment(client, audit)
                experiment.account = "fixture"
                with self.assertRaises(TimeoutError):
                    await experiment.write_once("TIGHTEN", "PUT", "/positions/fixture", {})
                with self.assertRaisesRegex(ValueError, "ALREADY_ATTEMPTED"):
                    await experiment.write_once("TIGHTEN", "PUT", "/positions/fixture", {})
                client._request.assert_awaited_once()
                audit.seek(0)
                self.assertNotIn("private-token", audit.read())

    async def test_existing_position_blocks_all_writes(self):
        client = AsyncMock()
        client.session.return_value = {"accountId": "fixture", "currency": "USD"}
        client.positions.return_value = {"positions": [{"position": {"dealId": "user-owned"}}]}
        client.working_orders.return_value = {"workingOrders": []}
        with tempfile.TemporaryDirectory() as root:
            with (Path(root)/"audit.jsonl").open("w", encoding="utf-8") as audit:
                with self.assertRaisesRegex(ValueError, "EMPTY_ACCOUNT"):
                    await Experiment(client, audit).run()
        client._request.assert_not_awaited()
