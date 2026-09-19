"""離線券商：以 bid/ask 與不利滑價成交。不是 Capital.com 成交模擬保證。"""
from dataclasses import dataclass
from .core import D, Direction, Rejected


@dataclass
class Position:
    direction: Direction
    size: D
    entry: D
    stop: D
    initial_distance: D
    value_per_point: D
    partial_taken: bool = False
    original_risk: D = D(0)
    initial_entry: D = D(0)
    opened_at: object = None
    mode: object = None
    added: bool = False


class PaperBroker:
    def __init__(self, balance=D("10000"), slippage=D("0.10")):
        self.balance = balance
        self.slippage = slippage
        self.position = None
        self.submissions = 0

    def open(self, plan, contract):
        if self.position:
            raise Rejected("POSITION_EXISTS")
        self.submissions += 1
        fill = plan.entry + self.slippage * plan.signal.direction.sign
        self.position = Position(plan.signal.direction, plan.size, fill, plan.signal.stop,
                                 plan.risk / plan.size + self.slippage, contract.value_per_point,
                                 original_risk=plan.risk, initial_entry=fill,
                                 opened_at=plan.signal.created, mode=plan.signal.mode)
        return fill

    def add(self, plan):
        p = self.position
        if not p or p.added or plan.signal.direction != p.direction or plan.size <= 0:
            raise Rejected("INVALID_ADD_ON")
        if (plan.signal.stop - p.stop) * p.direction.sign < 0:
            raise Rejected("STOP_CANNOT_LOOSEN")
        fill = plan.entry + self.slippage * p.direction.sign
        total = p.size + plan.size
        p.entry = (p.entry * p.size + fill * plan.size) / total
        p.size, p.stop, p.added = total, plan.signal.stop, True
        self.submissions += 1
        return fill

    def tighten(self, stop):
        p = self.position
        if not p or not stop.is_finite() or (stop - p.stop) * p.direction.sign < 0:
            raise Rejected("STOP_CANNOT_LOOSEN")
        p.stop = stop

    def close(self, quote, fraction=D(1)):
        p = self.position
        if not p or not quote.valid() or not 0 < fraction <= 1:
            raise Rejected("INVALID_CLOSE")
        size = p.size * fraction
        fill = quote.exit(p.direction) - self.slippage * p.direction.sign
        pnl = (fill - p.entry) * p.direction.sign * size * p.value_per_point
        self.balance += pnl
        p.size -= size
        if p.size == 0:
            self.position = None
        return pnl
