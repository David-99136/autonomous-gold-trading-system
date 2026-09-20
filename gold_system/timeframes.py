"""已核准收棒資料的多週期接線；不解析未驗證的券商原始時間。"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re

from .analysis import continuous
from .core import Bar, D
from .replay import aggregate
from .service import MarketFrame


@dataclass(frozen=True)
class ClosedMinute:
    bid: Bar
    ask: Bar
    received_at: datetime
    evidence_hash: str
    epic: str
    # 預設不合格；可信上游須有時間端點及收棒證據，不能只刪除尾列就改 True。
    closure_verified: bool = False


@dataclass(frozen=True)
class Timeframes:
    bid_m1: tuple
    ask_m1: tuple
    bid_m5: tuple
    ask_m5: tuple
    bid_h1: tuple
    ask_h1: tuple
    reasons: tuple

    @property
    def ready(self):
        return not self.reasons


class ClosedMinuteBuffer:
    """只供 GOLD Demo 的已驗證資料；本身不含憑證、網路或交易權限。"""

    def __init__(self, *, retention=2880):
        if type(retention) is not int or not 240 <= retention <= 10080:
            raise ValueError("BAR_RETENTION_INVALID")
        self.retention = retention
        self._minutes = {}
        self._conflict = False

    def ingest(self, minute):
        if not isinstance(minute, ClosedMinute) or minute.closure_verified is not True:
            raise ValueError("BAR_CLOSURE_UNVERIFIED")
        if minute.epic != "GOLD":
            raise ValueError("BAR_EPIC_MISMATCH")
        if not isinstance(minute.evidence_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", minute.evidence_hash):
            raise ValueError("BAR_EVIDENCE_MISSING")
        try:
            bid, ask, received = minute.bid, minute.ask, minute.received_at
            valid = (isinstance(bid, Bar) and isinstance(ask, Bar) and bid.valid() and ask.valid()
                     and bid.timestamp.utcoffset() is not None and ask.timestamp.utcoffset() is not None
                     and bid.timestamp == ask.timestamp and not bid.timestamp.second and not bid.timestamp.microsecond
                     and received.utcoffset() is not None and received >= bid.timestamp
                     and all(getattr(bid, k) <= getattr(ask, k) for k in ("open", "high", "low", "close")))
        except (TypeError, AttributeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("BAR_SCHEMA_INVALID")
        end = bid.timestamp.astimezone(timezone.utc)
        # UTC 端點也必須整分，不能接受帶秒數的奇異 timezone offset。
        if end.second or end.microsecond:
            raise ValueError("BAR_UTC_ALIGNMENT_INVALID")
        minute = replace(minute, bid=replace(bid, timestamp=end), ask=replace(ask, timestamp=end),
                         received_at=received.astimezone(timezone.utc))
        old = self._minutes.get(end)
        if old is not None:
            if old.bid != minute.bid or old.ask != minute.ask:
                self._conflict = True
                raise ValueError("BAR_REVISION_REQUIRES_RECOVERY")
            return False  # 不用重複資料的 received_at 改寫首次可用時間。
        self._minutes[end] = minute
        if len(self._minutes) > self.retention:
            del self._minutes[min(self._minutes)]
        return True

    def snapshot(self, now):
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("BAR_DECISION_TIME_INVALID")
        now = now.astimezone(timezone.utc)
        eligible = [b for _, b in sorted(self._minutes.items())
                    if b.bid.timestamp <= now and b.received_at <= now]
        bid, ask = tuple(b.bid for b in eligible), tuple(b.ask for b in eligible)
        bid5, ask5 = tuple(aggregate(bid, 5)), tuple(aggregate(ask, 5))
        bid60, ask60 = tuple(aggregate(bid, 60)), tuple(aggregate(ask, 60))
        # 只保留最近連續的 H1 尾段，避免把早期缺口傳給要求連續歷史的分析器。
        left = max(0, len(bid60) - 24)
        for index in range(len(bid60) - 1, left, -1):
            if (bid60[index].timestamp - bid60[index - 1].timestamp).total_seconds() != 3600:
                left = index
                break
        bid60, ask60 = bid60[left:], ask60[left:]
        reasons = []
        if self._conflict:
            reasons.append("BAR_REVISION_REQUIRES_RECOVERY")
        # 分析需要最近 3H、12 根 M5、2 根 M1；較舊缺口不永遠毒化後來完整窗口。
        for name, bars, count, minutes in (("M1", bid, 2, 1), ("M5", bid5, 12, 5), ("H1", bid60, 3, 60)):
            if len(bars) < count or not continuous(bars[-count:], minutes, now):
                reasons.append(name + "_INCOMPLETE_OR_STALE")
        return Timeframes(bid[-2:], ask[-2:], bid5[-12:], ask5[-12:], bid60[-24:], ask60[-24:], tuple(reasons))

    def frame(self, quote, quality, now):
        bars = self.snapshot(now)
        try:
            quote_ok = quote.valid() and 0 <= (now - quote.timestamp).total_seconds() <= 2
        except (TypeError, AttributeError, ValueError):
            quote_ok = False
        ready = bars.ready and quote_ok
        hourly_range = (max(b.high for b in bars.bid_m5) - min(b.low for b in bars.bid_m5)
                        if bars.ready else D(0))
        # 不提高上游其他品質權限；週末／時段、价差與時鐘檢查仍由原品質來源決定。
        quality = replace(quality, data_complete=quality.data_complete is True and ready,
                          hourly_range=hourly_range)
        return MarketFrame(quote, quality, bars.bid_h1, bars.bid_m5, bars.bid_m1)
