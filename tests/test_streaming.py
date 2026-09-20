import asyncio
import json
import unittest
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from gold_system.streaming import FeedError, NoRedirectConnect, decode, gold_quotes, parse_quote, STREAM_URL


NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
ACK = {"status": "OK", "destination": "marketData.subscribe", "correlationId": "gold-1",
       "payload": {"subscriptions": {"GOLD": "PROCESSED"}}}


def tick(**changes):
    payload = dict(epic="GOLD", product="CFD", bid=4000, ofr=4000.5,
                   timestamp=int(NOW.timestamp()*1000))
    payload.update(changes)
    return dict(status="OK", destination="quote", payload=payload)


class Socket:
    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        if self.messages:
            value = self.messages.pop(0)
            if isinstance(value, Exception):
                raise value
            return json.dumps(value)
        await asyncio.Event().wait()


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_and_decimal_quote_close_cleanly(self):
        ws, calls = Socket([ACK, tick()]), []
        def connector(uri, **kwargs):
            calls.append((uri, kwargs))
            return ws
        async with aclosing(gold_quotes({"CST": "test", "X-SECURITY-TOKEN": "test"},
                            connector=connector, clock=lambda: NOW)) as feed:
            quote = await anext(feed)
            self.assertEqual(str(quote.ask), "4000.5")
        self.assertTrue(ws.closed)
        self.assertEqual(calls[0][0], STREAM_URL)
        self.assertIsNone(calls[0][1]["proxy"])
        self.assertEqual(ws.sent[0]["payload"], {"epics": ["GOLD"]})

    async def test_silence_closes_connection(self):
        ws, events = Socket([ACK]), []
        feed = gold_quotes({"CST": "test", "X-SECURITY-TOKEN": "test"},
                           connector=lambda *a, **k: ws, on_event=events.append, silence_timeout=.01)
        with self.assertRaisesRegex(FeedError, "STREAM_QUOTE_TIMEOUT"):
            await anext(feed)
        self.assertTrue(ws.closed)
        self.assertIn("STREAM_CLOSED", events)

    async def test_secret_transport_exception_is_sanitized(self):
        ws = Socket([RuntimeError("secret-canary")])
        with self.assertRaisesRegex(FeedError, "^STREAM_CONNECTION_FAILED$"):
            await anext(gold_quotes({"CST": "test", "X-SECURITY-TOKEN": "test"},
                                   connector=lambda *a, **k: ws))
        self.assertTrue(ws.closed)

    async def test_rejected_subscription_never_yields(self):
        ws = Socket([{**ACK, "payload": {"subscriptions": {"GOLD": "REJECTED"}}}, tick()])
        with self.assertRaisesRegex(FeedError, "STREAM_SUBSCRIPTION_REJECTED"):
            await anext(gold_quotes({"CST": "test", "X-SECURITY-TOKEN": "test"},
                                   connector=lambda *a, **k: ws))

    def test_bad_prices_and_symbols_rejected(self):
        for changes in ({"bid": "NaN"}, {"ofr": 3999}, {"epic": "SILVER"},
                        {"timestamp": True}, {"timestamp": "1789776000000"}):
            with self.subTest(changes=changes), self.assertRaises(FeedError):
                parse_quote(tick(**changes), NOW)

    def test_stale_future_and_duplicate_rejected(self):
        for received, previous in ((NOW+timedelta(seconds=3), None),
                                   (NOW-timedelta(seconds=1), None), (NOW, NOW)):
            with self.assertRaises(FeedError):
                parse_quote(tick(), received, previous)

    def test_invalid_json_and_error_response_sanitized(self):
        for raw in ('{"secret":"canary"}', '[]', 'broken', 'x'*65537):
            with self.assertRaisesRegex(FeedError, "^STREAM_INVALID_MESSAGE$"):
                decode(raw)

    def test_redirect_not_followed(self):
        error = RuntimeError("redirect")
        self.assertIs(NoRedirectConnect.process_redirect(None, error), error)
