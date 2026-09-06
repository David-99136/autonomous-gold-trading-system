"""讀取真實 bid/ask 一分鐘 OHLC，逐棒重播；不插值、不偷看下一根。

OHLC 無法知道棒內順序。持倉一律先走不利極值再走有利極值，避免同棒停利/停損樂觀偏差。
這不是逐 tick 成交重建；產出會永久標記 BAR_REPLAY_ONLY。
"""
import csv
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from .analysis import analyze
from .broker import PaperBroker
from .core import Bar, Contract, D, Direction, Quote, Quality, RiskPolicy
from .engine import Engine
from .store import Store
from .validation import performance


@dataclass(frozen=True)
class Candle:
    bid: Bar
    ask: Bar
    tradeable: bool
    liquid: bool
    boundary: datetime
    spread_limit: D
    extreme: bool


def read_candles(path):
    """CSV 必須提供帶時區的收盤時間，價差門檻必須為當時已知的值。"""
    result = []
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            ts = datetime.fromisoformat(row["timestamp"])
            boundary = datetime.fromisoformat(row["boundary"])
            if ts.tzinfo is None or boundary.tzinfo is None:
                raise ValueError("CSV timestamps need timezone offsets")
            ts, boundary = ts.astimezone(timezone.utc), boundary.astimezone(timezone.utc)
            if ts.second or ts.microsecond:
                raise ValueError("One-minute close timestamps must align to minute boundaries")
            bars = [Bar(ts, *(D(row[f"{side}_{k}"]) for k in ("open", "high", "low", "close")))
                    for side in ("bid", "ask")]
            if not all(b.valid() for b in bars):
                raise ValueError("Invalid OHLC")
            if any(getattr(bars[0], k) > getattr(bars[1], k) for k in ("open", "high", "low", "close")):
                raise ValueError("Crossed bid/ask OHLC")
            if result and ts <= result[-1].bid.timestamp:
                raise ValueError("Duplicate or unordered timestamps")
            def flag(name):
                if row[name] not in ("true", "false"):
                    raise ValueError(f"{name} must be true or false")
                return row[name] == "true"
            limit = D(row["spread_limit"])
            if not limit.is_finite() or limit < 0:
                raise ValueError("Invalid spread limit")
            result.append(Candle(*bars, flag("tradeable"), flag("liquid_window"), boundary,
                                 limit, flag("extreme_volatility")))
    if not result:
        raise ValueError("Empty CSV")
    return result


def aggregate(bars, minutes):
    """僅在完整、連續且 UTC 邊界對齊時產生 5m/1H K 棒。"""
    groups = {}
    for b in bars:
        end = int(b.timestamp.timestamp())
        bucket = ((end - 1) // (minutes * 60) + 1) * minutes * 60
        groups.setdefault(bucket, []).append(b)
    output = []
    for bucket, group in groups.items():
        expected = [bucket - (minutes - 1 - i) * 60 for i in range(minutes)]
        if len(group) != minutes or [int(b.timestamp.timestamp()) for b in group] != expected:
            continue
        output.append(Bar(group[-1].timestamp, group[0].open, max(b.high for b in group),
                          min(b.low for b in group), group[-1].close))
    return output


async def replay_csv(csv_path, contract_path, output, balance=D(10000)):
    bars = read_candles(csv_path)
    contract_data = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    contract = Contract(contract_data["epic"], **{k: D(str(v)) for k, v in contract_data.items() if k != "epic"})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "events.db").exists():
        raise ValueError("Use a new output directory")
    store = Store(output / "events.db")
    policy, broker = RiskPolicy(), PaperBroker(balance)
    engine = Engine(store, broker, contract, policy)
    history, equity_curve, trade_pnl = [], [balance], []
    trade_start = None
    previous = None
    try:
        for candle in bars:
            now = candle.bid.timestamp - timedelta(minutes=1)
            # 只有上一分鐘的已收盤資訊進入分析。
            if previous and candle.bid.timestamp - previous != timedelta(minutes=1):
                history.clear()
                store.emit("DATA_GAP", simulated_at=now)
            m5, h1 = aggregate(history[-180:], 5), aggregate(history[-1800:], 60)
            signal = analyze(h1, m5[-12:], history[-2:], now)
            range_hour = max((b.high for b in m5[-12:]), default=D(0)) - min((b.low for b in m5[-12:]), default=D(0))
            quality = Quality(candle.tradeable, candle.liquid, len(m5) >= 12 and len(h1) >= 3,
                              candle.extreme, candle.spread_limit, 0,
                              D(str((candle.boundary - now).total_seconds() / 60)), range_hour,
                              False, True, True)
            open_quote = Quote(now, candle.bid.open, candle.ask.open)
            had_position = broker.position is not None
            before = broker.balance
            trailing = None
            if broker.position and m5:
                trailing = min(b.low for b in m5[-3:]) if broker.position.direction == Direction.LONG else max(b.high for b in m5[-3:])
            await engine.tick(now, open_quote, quality, signal, trailing)
            if not had_position and broker.position:
                trade_start = before
            # 不利端先觸發 stop。滑價仍由 PaperBroker 再加。
            fields = ("low", "high") if not broker.position or broker.position.direction == Direction.LONG else ("high", "low")
            for idx, field in enumerate((*fields, "close"), 1):
                at = now + timedelta(seconds=idx * 15)
                q = Quote(at, getattr(candle.bid, field), getattr(candle.ask, field))
                await engine.tick(at, q, replace(quality, minutes_to_boundary=D(str((candle.boundary-at).total_seconds()/60))))
                unrealized = D(0)
                if broker.position:
                    p = broker.position
                    unrealized = (q.exit(p.direction) - p.entry) * p.direction.sign * p.size * p.value_per_point
                equity_curve.append(broker.balance + unrealized)
            if trade_start is not None and not broker.position:
                trade_pnl.append(broker.balance - trade_start)
                trade_start = None
            history.append(candle.bid)
            history = history[-1800:]
            previous = candle.bid.timestamp
        if broker.position:
            last = bars[-1]
            pnl = broker.close(Quote(last.bid.timestamp, last.bid.close, last.ask.close))
            store.emit("EXIT", reason="REPLAY_END", pnl=pnl)
            if trade_start is not None:
                trade_pnl.append(broker.balance - trade_start)
            equity_curve.append(broker.balance)
        metrics = performance(trade_pnl, equity_curve)
        report = {"type": "BAR_REPLAY_ONLY", "qualified": False,
                  "limitations": ["OHLC_INTRABAR_ORDER_ASSUMED_ADVERSE_FIRST", "NO_AI_NEWS", "UNVALIDATED_PARAMETERS",
                                  "EXTERNAL_QUALITY_FLAGS_REQUIRE_POINT_IN_TIME_PROVENANCE"],
                  "input_sha256": sha256(Path(csv_path).read_bytes()).hexdigest(),
                  "contract_sha256": sha256(Path(contract_path).read_bytes()).hexdigest(),
                  "bars": len(bars), "metrics": metrics, "balance": broker.balance,
                  "equity_curve": equity_curve, "events": store.events()}
        (output / "report.json").write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        store.close()
