"""確定性執行迴圈。安全退出優先於新訊號；介面可隨時關閉。"""
import asyncio
from dataclasses import replace
from .core import D, Direction, Rejected
from .risk import make_plan, make_add_plan, validate_signal


class Engine:
    def __init__(self, store, broker, contract, policy, cost_ledger=None):
        if broker.slippage != policy.slippage:
            raise ValueError("Broker and risk slippage assumptions must match")
        self.store, self.broker, self.contract, self.policy = store, broker, contract, policy
        self.cost_ledger = cost_ledger
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

    def _valid_reverse(self, signal, position, now, quote, quality):
        if (signal is None or signal.direction not in (Direction.LONG, Direction.SHORT)
                or signal.direction == position.direction or self.state != "PAPER_READY"):
            return False
        try:
            validate_signal(signal, self.policy, now)
            if not quality.data_complete or not quality.analysis_available or not quality.tradeable:
                raise Rejected("REVERSE_DATA_UNAVAILABLE")
            if abs(quality.clock_offset_ms) > 1000:
                raise Rejected("CLOCK_DRIFT")
            if ((quote.exit(signal.direction) - signal.stop) * signal.direction.sign <= 0
                    or (signal.target - quote.entry(signal.direction)) * signal.direction.sign <= 0):
                raise Rejected("REVERSE_INVALIDATED")
            return True
        except Rejected as exc:
            self.store.emit("REVERSE_REJECTED", reason=str(exc), signal_id=signal.signal_id, simulated_at=now)
            return False

    async def tick(self, now, quote, quality, signal=None, trailing_stop=None, add_confirmation=None):
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
                elif (self._valid_reverse(signal, p, now, quote, quality)
                      and self.store.claim("reverse:" + signal.signal_id)):
                    # 平倉與之後的新倉是不同意圖。此 tick 只平倉，不同時反手。
                    intent = "reverse:" + signal.signal_id
                    try:
                        pnl = self.broker.close(quote)
                    except Exception:
                        self.state = "RECOVERY_LOCKED"
                        self.store.finish(intent, "UNKNOWN")
                        self.store.emit("RECOVERY_LOCKED", reason="REVERSE_OUTCOME_UNKNOWN",
                                        signal_id=signal.signal_id, simulated_at=now)
                        self._save()
                        return
                    self.store.finish(intent, "CONFIRMED")
                    book(pnl, "REVERSE_SIGNAL")
                elif not p.partial_taken and (price - p.initial_entry) * p.direction.sign >= p.initial_distance:
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
                if self.store.entries_stopped():
                    raise Rejected("OPERATOR_STOP_NEW")
                adding = self.broker.position is not None and add_confirmation is not None
                if self.broker.position and not adding:
                    raise Rejected("POSITION_EXISTS")
                if self.cost_ledger is not None:
                    # 費用資料故障只封鎖新倉；上面的硬停損與時間退出已先完成。
                    try:
                        available = self.cost_ledger.status(now)["analysis_available"]
                    except Exception:
                        raise Rejected("BUDGET_STATE_UNAVAILABLE") from None
                    quality = replace(quality, budget_available=quality.budget_available and available)
                if adding:
                    p = self.broker.position
                    equity = self.broker.balance + (quote.exit(p.direction) - p.entry) * p.direction.sign * p.size * p.value_per_point
                    plan = make_add_plan(signal, quote, quality, self.contract, self.policy, equity,
                                         now, p, add_confirmation)
                else:
                    plan = make_plan(signal, quote, quality, self.contract, self.policy, self.broker.balance, now)
                if not self.store.claim(signal.signal_id):
                    raise Rejected("DUPLICATE_INTENT")
                try:
                    fill = self.broker.add(plan) if adding else self.broker.open(plan, self.contract)
                    if not adding:
                        self.broker.position.opened_at = now
                except Exception:
                    self.state = "RECOVERY_LOCKED"
                    self.store.finish(signal.signal_id, "UNKNOWN")
                    self.store.emit("RECOVERY_LOCKED", signal_id=signal.signal_id)
                    return
                self.store.finish(signal.signal_id, "CONFIRMED")
                self.store.emit("ADD_ON" if adding else "ENTRY", signal_id=signal.signal_id, size=plan.size,
                                fill=fill, stop=signal.stop, strategy=signal.mode,
                                direction=signal.direction, simulated_at=now)
            except Rejected as exc:
                self.store.emit("NO_TRADE", reason=str(exc), signal_id=signal.signal_id, simulated_at=now)
