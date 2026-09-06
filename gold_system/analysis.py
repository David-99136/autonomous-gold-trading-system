"""可重播的多週期價格分析原型；門檻尚未經策略驗收，不具 Live 資格。"""
from datetime import timedelta
from hashlib import sha256
from dataclasses import dataclass
from .core import D, Direction, Mode, Signal


@dataclass(frozen=True)
class Zone:
    kind: str
    low: D
    high: D
    formed_at: object


def zones(h1):
    """前一根實體為 base，下一根收盤突破其完整區間才形成區域。

    後續收盤穿越遠端即失效；最多保留 24 根。這是可驗證的研究規則，非已校準策略。
    """
    found = []
    for i in range(1, len(h1)):
        base, impulse = h1[i - 1], h1[i]
        if len(h1) - i > 24:
            continue
        if impulse.close > base.high and impulse.close > impulse.open:
            if all(b.close >= base.low for b in h1[i + 1:]):
                found.append(Zone("DEMAND", base.low, max(base.open, base.close), impulse.timestamp))
        elif impulse.close < base.low and impulse.close < impulse.open:
            if all(b.close <= base.high for b in h1[i + 1:]):
                found.append(Zone("SUPPLY", min(base.open, base.close), base.high, impulse.timestamp))
    return found


def continuous(bars, minutes, now):
    return (bool(bars) and all(b.valid() and b.timestamp <= now for b in bars)
            and all(b.timestamp - a.timestamp == timedelta(minutes=minutes)
                    for a, b in zip(bars, bars[1:]))
            and timedelta(0) <= now - bars[-1].timestamp < timedelta(minutes=minutes))


def evidence_allowed(fields, optional_weights):
    """只供數值發布事件。質化新聞目前不可套用實際值欄位。"""
    required = ("source", "released_at", "received_at", "actual")
    if any(fields.get(k) is None or fields.get(k) == "" for k in required):
        return False
    if not optional_weights or any(not w.is_finite() or w <= 0 for w in optional_weights.values()):
        return False
    total = sum(optional_weights.values())
    missing = sum(w for k, w in optional_weights.items() if fields.get(k) is None)
    return missing / total < D("0.4")


def analyze(h1, m5, m1, now, version="prototype-v1-unvalidated"):
    """只消費已收盤資料；無足夠資料即輸出 NO_TRADE，而非補造。

    初版 continuation 為 HH/HL + 5m 突破 + 1m 確認。
    此函數供研究，不冒充已完成的 AI／新聞分析器。
    """
    key = sha256((version + now.isoformat() + repr((h1, m5, m1))).encode()).hexdigest()
    direction, stop, target, reason = Direction.NO_TRADE, D(0), D(0), "DATA_INCOMPLETE"
    mode = Mode.RIGHT
    if (len(h1) >= 3 and len(m5) >= 12 and len(m1) >= 2
            and continuous(h1, 60, now) and continuous(m5, 5, now) and continuous(m1, 1, now)):
        a, b = h1[-2:]
        last, previous = m5[-1], m5[-2]
        bullish = b.high > a.high and b.low > a.low
        bearish = b.high < a.high and b.low < a.low
        reason = "STRUCTURE_UNCONFIRMED"
        if bullish and last.close > previous.high and m1[-1].close > m1[-2].high:
            direction, stop = Direction.LONG, min(x.low for x in m5[-3:])
        elif bearish and last.close < previous.low and m1[-1].close < m1[-2].low:
            direction, stop = Direction.SHORT, max(x.high for x in m5[-3:])
        if direction == Direction.NO_TRADE:
            for zone in reversed(zones(h1)):
                # 先形成區域，再允許測試；不能事後替既有價格畫區。
                if zone.formed_at >= previous.timestamp:
                    continue
                touched = previous.low <= zone.high and previous.high >= zone.low
                if not touched:
                    continue
                if (zone.kind == "DEMAND" and last.close > previous.high
                        and m1[-1].close > m1[-2].high):
                    direction, stop, mode = Direction.LONG, min(zone.low, previous.low), Mode.LEFT
                    break
                if (zone.kind == "SUPPLY" and last.close < previous.low
                        and m1[-1].close < m1[-2].low):
                    direction, stop, mode = Direction.SHORT, max(zone.high, previous.high), Mode.LEFT
                    break
        if direction != Direction.NO_TRADE:
            entry = m1[-1].close
            target = entry + direction.sign * abs(entry - stop) * 2
            # 目標優先受已知反向區域約束，不能只用任意 2R 假造報酬空間。
            obstacles = [z.low if direction == Direction.LONG else z.high for z in zones(h1)
                         if (z.kind == "SUPPLY" if direction == Direction.LONG else z.kind == "DEMAND")
                         and ((z.low - entry) if direction == Direction.LONG else (entry - z.high)) > 0]
            if obstacles:
                target = min([target, *obstacles]) if direction == Direction.LONG else max([target, *obstacles])
            reason = f"PROTOTYPE_{mode.value}_NOT_VALIDATED"
    return Signal(key, version, now, now + timedelta(minutes=1), direction, mode, stop, target, reason)
