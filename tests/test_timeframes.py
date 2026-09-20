import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from gold_system.core import Bar, D, Quality, Quote
from gold_system.timeframes import ClosedMinute, ClosedMinuteBuffer


class TimeframeTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 21, 0, tzinfo=timezone.utc)
        self.now = self.start + timedelta(hours=3)
        self.buffer = ClosedMinuteBuffer(retention=240)

    def minute(self, i, *, delay=0):
        end = self.start + timedelta(minutes=i)
        base = D(2000) + D(i) / 100
        return ClosedMinute(Bar(end, base, base + 2, base - 1, base + 1),
                            Bar(end, base + D("0.5"), base + D("2.5"), base - D("0.5"), base + D("1.5")),
                            end + timedelta(seconds=delay), "a" * 64, "GOLD", True)

    def warmup(self, *, missing=()):
        for i in range(1, 181):
            if i not in missing:
                self.buffer.ingest(self.minute(i))

    def test_complete_frames_keep_bid_ask_and_utc_boundaries(self):
        self.warmup()
        result = self.buffer.snapshot(self.now)
        self.assertTrue(result.ready)
        self.assertEqual((len(result.bid_m1), len(result.bid_m5), len(result.bid_h1)), (2, 12, 3))
        self.assertEqual(result.bid_h1[0].timestamp, self.start + timedelta(hours=1))
        self.assertEqual(result.bid_h1[0].open, self.minute(1).bid.open)
        self.assertEqual(result.bid_h1[0].close, self.minute(60).bid.close)
        self.assertEqual(result.ask_h1[0].high - result.bid_h1[0].high, D("0.5"))

    def test_unverified_closure_and_missing_evidence_rejected(self):
        for minute in (replace(self.minute(1), closure_verified=False), replace(self.minute(1), evidence_hash="")):
            with self.assertRaises(ValueError):
                self.buffer.ingest(minute)

    def test_gap_does_not_produce_fake_complete_h1(self):
        self.warmup(missing=(100,))
        result = self.buffer.snapshot(self.now)
        self.assertFalse(result.ready)
        self.assertEqual(len(result.bid_h1), 1)  # 缺口前的 H1 不傳給連續尾段分析。
        self.assertIn("H1_INCOMPLETE_OR_STALE", result.reasons)

    def test_late_backfill_not_available_to_earlier_decision(self):
        self.warmup(missing=(100,))
        self.buffer.ingest(replace(self.minute(100), received_at=self.now + timedelta(seconds=1)))
        self.assertFalse(self.buffer.snapshot(self.now).ready)
        self.assertTrue(self.buffer.snapshot(self.now + timedelta(seconds=1)).ready)

    def test_duplicate_does_not_backdate_receipt(self):
        minute = replace(self.minute(180), received_at=self.now + timedelta(seconds=1))
        self.buffer.ingest(minute)
        self.assertFalse(self.buffer.ingest(self.minute(180)))
        self.assertEqual(self.buffer.snapshot(self.now).bid_m1, ())

    def test_conflicting_revision_locks_buffer(self):
        self.warmup()
        minute = self.minute(180)
        with self.assertRaisesRegex(ValueError, "REVISION"):
            self.buffer.ingest(replace(minute, bid=replace(minute.bid, close=minute.bid.close + D("0.1"))))
        self.assertFalse(self.buffer.snapshot(self.now).ready)

    def test_future_and_incomplete_minutes_do_not_leak(self):
        self.warmup()
        self.buffer.ingest(self.minute(181))
        result = self.buffer.snapshot(self.now)
        self.assertEqual(result.bid_m1[-1].timestamp, self.now)
        self.assertEqual(result.bid_m5[-1].timestamp, self.now)

    def test_crossed_prices_and_receipt_before_close_rejected(self):
        minute = self.minute(1)
        for broken in (replace(minute, ask=replace(minute.ask, close=minute.bid.close - 1)),
                       replace(minute, received_at=self.start)):
            with self.assertRaises(ValueError):
                self.buffer.ingest(broken)

    def test_frame_never_overrides_upstream_quality(self):
        self.warmup()
        quote = Quote(self.now, D(2000), D("2000.5"))
        frame = self.buffer.frame(quote, Quality(), self.now)
        self.assertFalse(frame.quality.data_complete)
        self.assertFalse(frame.quality.tradeable)
        good = self.buffer.frame(quote, Quality(data_complete=True), self.now)
        self.assertTrue(good.quality.data_complete)
        self.assertGreater(good.quality.hourly_range, 0)

    def test_stale_quote_or_bars_block_data_quality(self):
        self.warmup()
        quote = Quote(self.now - timedelta(seconds=3), D(2000), D("2000.5"))
        self.assertFalse(self.buffer.frame(quote, Quality(data_complete=True), self.now).quality.data_complete)
        self.assertFalse(self.buffer.snapshot(self.now + timedelta(minutes=1)).ready)

    def test_retention_is_bounded_and_out_of_order_backfill_allowed(self):
        for i in reversed(range(1, 301)):
            self.buffer.ingest(self.minute(i))
        self.assertEqual(len(self.buffer._minutes), 240)
        self.assertEqual(min(self.buffer._minutes), self.start + timedelta(minutes=61))

    def test_non_utc_input_is_normalized(self):
        minute = self.minute(1)
        local = minute.bid.timestamp.astimezone(timezone(timedelta(hours=8)))
        self.buffer.ingest(replace(minute, bid=replace(minute.bid, timestamp=local), ask=replace(minute.ask, timestamp=local)))
        self.assertEqual(self.buffer.snapshot(local).bid_m1[-1].timestamp.utcoffset(), timedelta(0))

    def test_eth_cannot_enter_gold_buffer(self):
        with self.assertRaisesRegex(ValueError, "EPIC_MISMATCH"):
            self.buffer.ingest(replace(self.minute(1), epic="ETHUSD"))

    def test_old_gap_is_excluded_from_continuous_h1_tail(self):
        from gold_system.analysis import continuous
        self.buffer = ClosedMinuteBuffer(retention=480)
        for i in range(1, 361):
            if i != 100:
                self.buffer.ingest(self.minute(i))
        now = self.start + timedelta(hours=6)
        result = self.buffer.snapshot(now)
        self.assertTrue(result.ready)
        self.assertTrue(continuous(result.bid_h1, 60, now))
        self.assertEqual(len(result.bid_h1), 4)
