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
    return await replay_candles(bars, contract, output, balance,
        input_sha256=sha256(Path(csv_path).read_bytes()).hexdigest(),
        contract_sha256=sha256(Path(contract_path).read_bytes()).hexdigest())


async def replay_candles(bars, contract, output, balance=D(10000), *, warmup=(),
                         input_sha256=None, contract_sha256=None):
    """共用重播核心；warmup 只建立已收盤指標，不交易、不計入 OOS 損益。"""
    if not bars or not isinstance(balance, D) or not balance.is_finite() or balance <= 0:
        raise ValueError("Replay needs candles and positive initial equity")
    previous_time = None
    for candle in (*warmup, *bars):
        if (not candle.bid.valid() or not candle.ask.valid()
                or candle.bid.timestamp != candle.ask.timestamp
                or candle.bid.timestamp.tzinfo is None
                or candle.bid.timestamp.second or candle.bid.timestamp.microsecond
                or any(getattr(candle.bid, k) > getattr(candle.ask, k) for k in ("open", "high", "low", "close"))
                or (previous_time is not None and candle.bid.timestamp <= previous_time)):
            raise ValueError("Invalid or overlapping replay/warmup candles")
        previous_time = candle.bid.timestamp
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "events.db").exists():
        raise ValueError("Use a new output directory")
    store = Store(output / "events.db")
    policy, broker = RiskPolicy(), PaperBroker(balance)
    engine = Engine(store, broker, contract, policy)
    history, equity_curve, trade_pnl = [], [balance], []
    equity_points = [(bars[0].bid.timestamp-timedelta(minutes=1), balance)]
    trade_start = None
    active_path, trade_paths = None, []
    previous = None
    for candle in warmup:
        if previous and candle.bid.timestamp-previous != timedelta(minutes=1):
            history.clear()
        history.append(candle.bid)
        history = history[-1800:]
        previous = candle.bid.timestamp
    def mark(when, quote):
        nonlocal trade_start, active_path
        unrealized = D(0)
        if broker.position:
            p = broker.position
            unrealized = (quote.exit(p.direction)-p.entry)*p.direction.sign*p.size*p.value_per_point
        equity = broker.balance+unrealized
        equity_curve.append(equity)
        # 開窗資金與同時發生的成交價差必須分開保留，不覆蓋起始資金。
        observed_at = when if when > equity_points[-1][0] else equity_points[-1][0]+timedelta(microseconds=1)
        equity_points.append((observed_at, equity))
        if active_path is not None:
            active_path["returns"].append((equity-trade_start)/trade_start)
            if broker.position is None:
                active_path["closed_at"] = when.isoformat()
                active_path["net_pnl"] = broker.balance-trade_start
                trade_pnl.append(active_path["net_pnl"])
                trade_paths.append(active_path)
                trade_start, active_path = None, None
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
            await engine.tick(now, open_quote, quality, signal, trailing,
                              add_confirmation=m5[-1] if had_position and m5 else None)
            if not had_position and broker.position:
                trade_start = before
                active_path = {"opened_at": now.isoformat(), "direction": str(signal.direction),
                               "strategy_mode": str(signal.mode), "start_equity": before, "returns": [D(0)]}
            mark(now, open_quote)
            # 不利端先觸發 stop。滑價仍由 PaperBroker 再加。
            fields = ("low", "high") if not broker.position or broker.position.direction == Direction.LONG else ("high", "low")
            for idx, field in enumerate((*fields, "close"), 1):
                at = now + timedelta(seconds=idx * 15)
                q = Quote(at, getattr(candle.bid, field), getattr(candle.ask, field))
                await engine.tick(at, q, replace(quality, minutes_to_boundary=D(str((candle.boundary-at).total_seconds()/60))))
                mark(at, q)
            history.append(candle.bid)
            history = history[-1800:]
            previous = candle.bid.timestamp
        last = bars[-1]
        if broker.position:
            pnl = broker.close(Quote(last.bid.timestamp, last.bid.close, last.ask.close))
            store.emit("EXIT", reason="REPLAY_END", pnl=pnl)
        mark(last.bid.timestamp, Quote(last.bid.timestamp, last.bid.close, last.ask.close))
        metrics = performance(trade_pnl, equity_curve)
        report = {"type": "BAR_REPLAY_ONLY", "qualified": False,
                  "limitations": ["OHLC_INTRABAR_ORDER_ASSUMED_ADVERSE_FIRST", "NO_AI_NEWS", "UNVALIDATED_PARAMETERS",
                                  "EXTERNAL_QUALITY_FLAGS_REQUIRE_POINT_IN_TIME_PROVENANCE"],
                  "input_sha256": input_sha256,
                  "contract_sha256": contract_sha256,
                  "bars": len(bars), "metrics": metrics, "balance": broker.balance,
                  "equity_curve": equity_curve, "equity_points": equity_points,
                  "trade_paths": trade_paths, "events": store.events()}
        (output / "report.json").write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        store.close()
