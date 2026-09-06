"""確定性執行迴圈。安全退出優先於新訊號；介面可隨時關閉。"""
import asyncio
from .core import D, Rejected
from .risk import make_plan


class Engine:
    def __init__(self, store, broker, contract, policy):
        self.store, self.broker, self.contract, self.policy = store, broker, contract, policy
        # 重啟不猜測上次部位是否存在，必須由操作者重播或對帳。
        self.state = "RECOVERY_LOCKED" if store.get("started", False) else "PAPER_READY"
        store.set("started", True)
        self.lock = asyncio.Lock()
        self.day = store.get("day")
        self.day_equity = D(store.get("day_equity", str(broker.balance)))
        self.realized = D(store.get("realized", "0"))
        self.soft = store.get("soft", False)
        self.hard = store.get("hard", False)

    def _save(self):
        for k in ("day", "day_equity", "realized", "soft", "hard"):
            self.store.set(k, getattr(self, k))

    async def tick(self, now, quote, quality, signal=None, trailing_stop=None):
        async with self.lock:
            if not quote.valid() or not 0 <= (now - quote.timestamp).total_seconds() <= 2:
                self.store.emit("DATA_INVALID", simulated_at=now)
                return
            day = now.date().isoformat()  # 原型使用 UTC 日；帳戶交易日仍為待驗證設定。
            if day != self.day:
                if self.broker.position:
                    self.state = "RECOVERY_LOCKED"
                self.day, self.day_equity, self.realized = day, self.broker.balance, D(0)
                self.soft = False  # hard 不可透過跨日解除。
            if self.day_equity <= 0:
                self.state = "RECOVERY_LOCKED"
                return

            def book(pnl, reason):
                self.realized += pnl
                self.store.emit("EXIT", pnl=pnl, reason=reason, simulated_at=now)
                self.soft |= self.realized <= -self.day_equity * self.policy.soft_loss
                self.hard |= self.realized <= -self.day_equity * self.policy.hard_loss

            self.soft |= self.realized <= -self.day_equity * self.policy.soft_loss
            self.hard |= self.realized <= -self.day_equity * self.policy.hard_loss
            p = self.broker.position
            had_position = p is not None
            if p:
                price = quote.exit(p.direction)
                if self.hard or quality.minutes_to_boundary <= 10 or (price - p.stop) * p.direction.sign <= 0:
                    reason = "HARD_LOSS" if self.hard else "SESSION_CLOSE" if quality.minutes_to_boundary <= 10 else "STOP"
                    book(self.broker.close(quote), reason)
                elif not p.partial_taken and (price - p.entry) * p.direction.sign >= p.initial_distance:
                    book(self.broker.close(quote, D("0.5")), "ONE_R_HALF")
                    p.partial_taken = True
                    self.broker.tighten(p.entry + self.broker.slippage * p.direction.sign)
                elif p.partial_taken and trailing_stop is not None:
                    if ((price - trailing_stop) * p.direction.sign > 0
                            and (trailing_stop - p.stop) * p.direction.sign >= 0):
                        self.broker.tighten(trailing_stop)
            if self.hard:
                self.state = "HARD_LOCKED"
            self._save()
            if signal is None:
                return
            try:
                if had_position and self.broker.position is None:
                    raise Rejected("WAIT_AFTER_EXIT")
                if self.state != "PAPER_READY" or self.soft or self.hard:
                    raise Rejected("ENTRY_LOCKED")
                if self.broker.position:
                    raise Rejected("POSITION_EXISTS")
                plan = make_plan(signal, quote, quality, self.contract, self.policy, self.broker.balance, now)
                if not self.store.claim(signal.signal_id):
                    raise Rejected("DUPLICATE_INTENT")
                try:
                    fill = self.broker.open(plan, self.contract)
                except Exception:
                    self.state = "RECOVERY_LOCKED"
                    self.store.finish(signal.signal_id, "UNKNOWN")
                    self.store.emit("RECOVERY_LOCKED", signal_id=signal.signal_id)
                    return
                self.store.finish(signal.signal_id, "CONFIRMED")
                self.store.emit("ENTRY", signal_id=signal.signal_id, size=plan.size,
                                fill=fill, stop=signal.stop, strategy=signal.mode,
                                direction=signal.direction, simulated_at=now)
            except Rejected as exc:
                self.store.emit("NO_TRADE", reason=str(exc), signal_id=signal.signal_id, simulated_at=now)
