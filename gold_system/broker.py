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
                                 abs(fill - plan.signal.stop), contract.value_per_point)
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
