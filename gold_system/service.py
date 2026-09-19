"""雙代理的非同步 Paper 協調器；行情管理不等待分析，無券商認證或網路寫入。"""
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .broker import PaperBroker
from .analysis import continuous
from .core import Bar, Direction, Quality, Quote, Signal


@dataclass(frozen=True)
class MarketFrame:
    quote: Quote
    quality: Quality
    h1: tuple[Bar, ...] = ()
    m5: tuple[Bar, ...] = ()
    m1: tuple[Bar, ...] = ()


class PaperService:
    """analysis(frame) 是非同步分析介面；正式 provider 可在此接費用預留／證據查核。

    同時最多一個分析請求。忙碌時只保留最新行情，避免把舊行情排成長佇列。
    run 結束會取消分析並保留稽核；不宣稱已平倉，重啟仍依 Engine 的恢復鎖處理。
    """
    def __init__(self, engine, analysis, *, analysis_timeout=2, clock=None):
        if not isinstance(engine.broker, PaperBroker):
            raise ValueError("This service supports PaperBroker only")
        if type(analysis_timeout) not in (int, float) or not 0 < analysis_timeout <= 60:
            raise ValueError("Analysis timeout must be positive and at most 60 seconds")
        self.engine, self.analysis = engine, analysis
        self.analysis_timeout = analysis_timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._pending = asyncio.Queue(maxsize=1)
        self._signal = None
        self._analysis_healthy = False
        self._running = False
        self._used = False
        self._last_quote = None
        self._last_request = None
        self._analysis_epoch = 0

    def _invalidate(self, reason):
        self._signal = None
        self._analysis_healthy = False
        self._analysis_epoch += 1
        while not self._pending.empty():
            self._pending.get_nowait()
        self.engine.store.emit("SERVICE_ENTRY_LOCKED", reason=reason)

    async def _analyze(self):
        while True:
            frame, epoch = await self._pending.get()
            try:
                # Timeout 包含 provider 的等待時間；provider 的未知費用仍由費用庫處理。
                result = await asyncio.wait_for(self.analysis(frame), timeout=self.analysis_timeout)
                now = self.clock()
                if (not isinstance(result, Signal) or result.version != self.engine.policy.version
                        or not isinstance(result.direction, Direction)
                        or not frame.quote.timestamp <= result.created <= now < result.expires):
                    raise ValueError("Invalid or expired analysis result")
                if epoch != self._analysis_epoch:
                    continue  # 故障前發出的分析不能在故障後重新開啟入場。
                self._signal, self._analysis_healthy = result, True
                self.engine.store.emit("ANALYSIS_READY", signal_id=result.signal_id,
                                       version=result.version, source_quote_at=frame.quote.timestamp)
            except asyncio.CancelledError:
                raise
            except Exception:
                if epoch == self._analysis_epoch:
                    self._signal, self._analysis_healthy = None, False
                # 不將 provider 原始例外（可能含 URL／認證資料）帶入一般稽核。
                self.engine.store.emit("ANALYSIS_UNAVAILABLE", source_quote_at=frame.quote.timestamp)

    async def _frame(self, frame):
        now = self.clock()
        if (not isinstance(frame, MarketFrame) or not frame.quote.valid()
                or not 0 <= (now-frame.quote.timestamp).total_seconds() <= 2
                or (self._last_quote is not None and frame.quote.timestamp <= self._last_quote)):
            self._invalidate("INVALID_OR_UNORDERED_FEED")
            return
        self._last_quote = frame.quote.timestamp
        histories_valid = all(not bars or (
            isinstance(bars, tuple) and continuous(bars, minutes, frame.quote.timestamp)
            and all(not b.timestamp.second and not b.timestamp.microsecond
                    and int(b.timestamp.timestamp()) % (minutes*60) == 0 for b in bars))
            for bars, minutes in ((frame.h1, 60), (frame.m5, 5), (frame.m1, 1)))
        if not histories_valid:
            self._invalidate("INVALID_CLOSED_BAR_HISTORY")
        signal = self._signal
        if signal is not None and not signal.created <= now < signal.expires:
            self._signal, self._analysis_healthy, signal = None, False, None
        quality = replace(frame.quality, analysis_available=frame.quality.analysis_available and self._analysis_healthy,
                          data_complete=frame.quality.data_complete and histories_valid)
        p = self.engine.broker.position
        closed_m5 = frame.m5 if histories_valid else ()
        trailing = None
        if p and len(closed_m5) >= 3:
            trailing = (min(b.low for b in closed_m5[-3:]) if p.direction == Direction.LONG
                        else max(b.high for b in closed_m5[-3:]))
        # 此 await 只執行確定性交易管理，從不等待模型或其他分析 I/O。
        await self.engine.tick(now, frame.quote, quality, signal, trailing,
            add_confirmation=closed_m5[-1] if closed_m5 else None)
        # 已實現熔斷、停止新倉或不合格行情時，不啟動新的分析工作。
        eligible = (self.engine.state == "PAPER_READY" and not self.engine.soft and not self.engine.hard
                    and not self.engine.store.entries_stopped() and frame.quality.analysis_available
                    and frame.quality.budget_available and quality.data_complete)
        # 分析以最新已收盤 M1 為單位去重；無 M1 時使用報價時間，供串流測試介面使用。
        request_time = frame.m1[-1].timestamp if frame.m1 else frame.quote.timestamp
        if eligible and request_time != self._last_request:
            self._last_request = request_time
            if self._pending.full():
                self._pending.get_nowait()
            self._pending.put_nowait((frame, self._analysis_epoch))

    async def run(self, feed):
        """feed 為 async iterator；呼叫者取消 run 可停止服務，模型任務會一起回收。"""
        if self._running or self._used:
            raise RuntimeError("Paper service instances are single-use; restart requires recovery")
        self._running = True
        self._used = True
        worker = asyncio.create_task(self._analyze())
        self.engine.store.emit("PAPER_SERVICE_STARTED")
        try:
            async for frame in feed:
                await self._frame(frame)
                await asyncio.sleep(0)  # 即使 feed 為記憶體資料也讓分析工作有機會執行。
        except asyncio.CancelledError:
            self._invalidate("SERVICE_CANCELLED")
            raise
        except Exception:
            self._invalidate("FEED_OR_ENGINE_FAILED")
            raise RuntimeError("PAPER_SERVICE_FAILED; inspect sanitized audit") from None
        finally:
            self._signal, self._analysis_healthy = None, False
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            self._running = False
            self.engine.store.emit("PAPER_SERVICE_STOPPED", position_open=self.engine.broker.position is not None)
