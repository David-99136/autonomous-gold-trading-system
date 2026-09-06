"""不可變交易資料；金額以 Decimal 避免二進位浮點數的下單誤差。"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

D = Decimal


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"

    @property
    def sign(self):
        return D(1) if self == self.LONG else D(-1)


class Mode(StrEnum):
    LEFT = "LEFT_REVERSAL"
    RIGHT = "RIGHT_CONTINUATION"


@dataclass(frozen=True)
class Bar:
    # timestamp 是收盤時間，不能把未完成 K 棒當作已知歷史。
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def valid(self):
        return (self.timestamp.tzinfo is not None
                and all(x.is_finite() and x > 0 for x in (self.open, self.high, self.low, self.close))
                and self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high)


@dataclass(frozen=True)
class Quote:
    timestamp: datetime
    bid: Decimal
    ask: Decimal

    @property
    def spread(self):
        return self.ask - self.bid

    def entry(self, direction):
        return self.ask if direction == Direction.LONG else self.bid

    def exit(self, direction):
        return self.bid if direction == Direction.LONG else self.ask

    def valid(self):
        return (self.timestamp.tzinfo is not None and self.bid.is_finite()
                and self.ask.is_finite() and 0 < self.bid <= self.ask)


@dataclass(frozen=True)
class Contract:
    epic: str
    value_per_point: Decimal
    minimum: Decimal
    increment: Decimal
    maximum: Decimal
    margin_rate: Decimal
    minimum_stop: Decimal

    def __post_init__(self):
        values = (self.value_per_point, self.minimum, self.increment, self.maximum,
                  self.margin_rate, self.minimum_stop)
        if (not self.epic or not all(x.is_finite() and x > 0 for x in values)
                or self.minimum > self.maximum or self.margin_rate > 1):
            raise ValueError("Invalid contract")


@dataclass(frozen=True)
class Signal:
    signal_id: str
    version: str
    created: datetime
    expires: datetime
    direction: Direction
    mode: Mode
    stop: Decimal
    target: Decimal
    reason: str


@dataclass(frozen=True)
class Quality:
    tradeable: bool = False
    liquid_window: bool = False
    data_complete: bool = False
    extreme_volatility: bool = True
    spread_limit: Decimal = D(0)
    clock_offset_ms: int = 1001
    minutes_to_boundary: Decimal = D(0)
    hourly_range: Decimal = D(0)
    trend_exception: bool = False
    analysis_available: bool = False
    budget_available: bool = False


@dataclass(frozen=True)
class Plan:
    signal: Signal
    entry: Decimal
    size: Decimal
    risk: Decimal
    margin: Decimal


@dataclass(frozen=True)
class RiskPolicy:
    risk_fraction: Decimal = D("0.0025")
    normal_margin: Decimal = D("0.20")
    absolute_margin: Decimal = D("0.70")
    soft_loss: Decimal = D("0.03")
    hard_loss: Decimal = D("0.10")
    minimum_rr: Decimal = D("1.5")
    slippage: Decimal = D("0.10")
    version: str = "prototype-v1-unvalidated"


class Rejected(ValueError):
    """可預期的拒單，必須保存到稽核紀錄。"""
