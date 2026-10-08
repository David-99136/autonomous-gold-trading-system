"""可重播的多週期價格分析原型；門檻尚未經策略驗收，不具 Live 資格。"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from dataclasses import asdict, dataclass, replace
import json
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
    # [Codex | 2026-09-27] Bar 的時間是 UTC 收盤邊界；連續但錯位的資料也不合格。
    return (isinstance(now, datetime) and now.utcoffset() is not None and bool(bars)
            and all(b.valid() and b.timestamp <= now
                    and not b.timestamp.second and not b.timestamp.microsecond
                    and b.timestamp.astimezone(timezone.utc).minute % minutes == 0 for b in bars)
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
    # [Antigravity | 2026-09-27] 預設 spec §4 欄位
    zone_id = ""
    confirmation = ""
    invalidation_price = D(0)
    rule_score = D(0)
    data_completeness = "INSUFFICIENT"

    if (len(h1) >= 3 and len(m5) >= 12 and len(m1) >= 2
            and continuous(h1, 60, now) and continuous(m5, 5, now) and continuous(m1, 1, now)):
        data_completeness = "COMPLETE"
        a, b = h1[-2:]
        last, previous = m5[-1], m5[-2]
        bullish = b.high > a.high and b.low > a.low
        bearish = b.high < a.high and b.low < a.low
        reason = "STRUCTURE_UNCONFIRMED"
        if bullish and last.close > previous.high and m1[-1].close > m1[-2].high:
            direction, stop = Direction.LONG, min(x.low for x in m5[-3:])
            zone_id = f"TREND_H1_BULL_{int(b.timestamp.timestamp())}"
            confirmation = "M5_BREAKOUT_M1_TRIGGER_LONG"
            rule_score = D("0.70")
        elif bearish and last.close < previous.low and m1[-1].close < m1[-2].low:
            direction, stop = Direction.SHORT, max(x.high for x in m5[-3:])
            zone_id = f"TREND_H1_BEAR_{int(b.timestamp.timestamp())}"
            confirmation = "M5_BREAKOUT_M1_TRIGGER_SHORT"
            rule_score = D("0.70")
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
                    zone_id = f"ZONE_{zone.kind}_{int(zone.formed_at.timestamp())}"
                    confirmation = "M5_REVERSAL_M1_TRIGGER_LONG"
                    rule_score = D("0.80")
                    break
                if (zone.kind == "SUPPLY" and last.close < previous.low
                        and m1[-1].close < m1[-2].low):
                    direction, stop, mode = Direction.SHORT, max(zone.high, previous.high), Mode.LEFT
                    zone_id = f"ZONE_{zone.kind}_{int(zone.formed_at.timestamp())}"
                    confirmation = "M5_REVERSAL_M1_TRIGGER_SHORT"
                    rule_score = D("0.80")
                    break
        if direction != Direction.NO_TRADE:
            entry = m1[-1].close
            invalidation_price = stop
            target = entry + direction.sign * abs(entry - stop) * 2
            # 目標優先受已知反向區域約束，不能只用任意 2R 假造報酬空間。
            obstacles = [z.low if direction == Direction.LONG else z.high for z in zones(h1)
                         if (z.kind == "SUPPLY" if direction == Direction.LONG else z.kind == "DEMAND")
                         and ((z.low - entry) if direction == Direction.LONG else (entry - z.high)) > 0]
            if obstacles:
                target = min([target, *obstacles]) if direction == Direction.LONG else max([target, *obstacles])
            reason = f"PROTOTYPE_{mode.value}_NOT_VALIDATED"
    return Signal(
        signal_id=key, version=version, created=now, expires=now + timedelta(minutes=1),
        direction=direction, mode=mode, stop=stop, target=target, reason=reason,
        # [Antigravity | 2026-09-27] 填入 spec §4 完整契約欄位
        zone_id=zone_id,
        confirmation_condition=confirmation,
        invalidation_price=invalidation_price,
        rule_score=rule_score,
        confidence=None,
        data_completeness=data_completeness,
    )


# [Antigravity | 2026-09-27] 候選訊號管線：同時評估 RIGHT_CONTINUATION 與 LEFT_REVERSAL 候選
def candidate_signals(h1, m5, m1, now, version="prototype-v1-unvalidated"):
    """為 CodexFrameAnalysis 產出結構性候選訊號列表；無合格訊號時回傳空 tuple。"""
    if not (len(h1) >= 3 and len(m5) >= 12 and len(m1) >= 2
            and continuous(h1, 60, now) and continuous(m5, 5, now) and continuous(m1, 1, now)):
        return ()

    candidates = []
    a, b = h1[-2:]
    last, previous = m5[-1], m5[-2]
    bullish = b.high > a.high and b.low > a.low
    bearish = b.high < a.high and b.low < a.low

    # 1. 右側趨勢延續 (RIGHT_CONTINUATION)
    right_dir, right_stop = None, None
    if bullish and last.close > previous.high and m1[-1].close > m1[-2].high:
        right_dir, right_stop = Direction.LONG, min(x.low for x in m5[-3:])
    elif bearish and last.close < previous.low and m1[-1].close < m1[-2].low:
        right_dir, right_stop = Direction.SHORT, max(x.high for x in m5[-3:])

    if right_dir is not None:
        entry = m1[-1].close
        if (right_dir == Direction.LONG and entry > right_stop) or (right_dir == Direction.SHORT and entry < right_stop):
            target = entry + right_dir.sign * abs(entry - right_stop) * 2
            obstacles = [z.low if right_dir == Direction.LONG else z.high for z in zones(h1)
                         if (z.kind == "SUPPLY" if right_dir == Direction.LONG else z.kind == "DEMAND")
                         and ((z.low - entry) if right_dir == Direction.LONG else (entry - z.high)) > 0]
            if obstacles:
                target = min([target, *obstacles]) if right_dir == Direction.LONG else max([target, *obstacles])
            if abs(target - entry) >= abs(entry - right_stop) * D("1.5"):
                candidates.append(Signal(
                    signal_id="", version=version, created=now, expires=now + timedelta(minutes=1),
                    direction=right_dir, mode=Mode.RIGHT, stop=right_stop, target=target,
                    reason="CANDIDATE_RIGHT_CONTINUATION",
                    zone_id=f"TREND_H1_{'BULL' if right_dir == Direction.LONG else 'BEAR'}_{int(b.timestamp.timestamp())}",
                    confirmation_condition=f"M5_BREAKOUT_M1_TRIGGER_{right_dir.value}",
                    invalidation_price=right_stop, rule_score=D("0.70"), confidence=None,
                    data_completeness="COMPLETE",
                ))

    # 2. 左側區域反轉 (LEFT_REVERSAL)
    for zone in reversed(zones(h1)):
        if zone.formed_at >= previous.timestamp:
            continue
        touched = previous.low <= zone.high and previous.high >= zone.low
        if not touched:
            continue
        left_dir, left_stop = None, None
        if zone.kind == "DEMAND" and last.close > previous.high and m1[-1].close > m1[-2].high:
            left_dir, left_stop = Direction.LONG, min(zone.low, previous.low)
        elif zone.kind == "SUPPLY" and last.close < previous.low and m1[-1].close < m1[-2].low:
            left_dir, left_stop = Direction.SHORT, max(zone.high, previous.high)
        if left_dir is not None:
            entry = m1[-1].close
            if (left_dir == Direction.LONG and entry > left_stop) or (left_dir == Direction.SHORT and entry < left_stop):
                target = entry + left_dir.sign * abs(entry - left_stop) * 2
                obstacles = [z.low if left_dir == Direction.LONG else z.high for z in zones(h1)
                             if (z.kind == "SUPPLY" if left_dir == Direction.LONG else z.kind == "DEMAND")
                             and ((z.low - entry) if left_dir == Direction.LONG else (entry - z.high)) > 0]
                if obstacles:
                    target = min([target, *obstacles]) if left_dir == Direction.LONG else max([target, *obstacles])
                if abs(target - entry) >= abs(entry - left_stop) * D("1.5"):
                    candidates.append(Signal(
                        signal_id="", version=version, created=now, expires=now + timedelta(minutes=1),
                        direction=left_dir, mode=Mode.LEFT, stop=left_stop, target=target,
                        reason="CANDIDATE_LEFT_REVERSAL",
                        zone_id=f"ZONE_{zone.kind}_{int(zone.formed_at.timestamp())}",
                        confirmation_condition=f"M5_REVERSAL_M1_TRIGGER_{left_dir.value}",
                        invalidation_price=left_stop, rule_score=D("0.80"), confidence=None,
                        data_completeness="COMPLETE",
                    ))
                    break

    # [Codex | 2026-09-27] 完整內容與證據綁定：資料修訂不能沿用舊意圖的 ID。
    history = [[asdict(bar) for bar in bars] for bars in (h1, m5, m1)]
    def identity(signal):
        payload = {"schema": "candidate-v2", "signal": asdict(signal), "history": history}
        payload["signal"].pop("signal_id")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
                             default=lambda v: v.astimezone(timezone.utc).isoformat()
                             if isinstance(v, datetime) else str(v))
        return replace(signal, signal_id=sha256(encoded.encode()).hexdigest())
    return tuple(identity(signal) for signal in candidates)


# [Antigravity | 2026-09-27] MarketFrame 轉接候選訊號
def frame_candidates(frame, version="prototype-v1-unvalidated"):
    """從 MarketFrame 擷取候選訊號，供 CodexFrameAnalysis 的 candidates callback 使用。"""
    h1 = getattr(frame, "h1", ())
    m5 = getattr(frame, "m5", ())
    m1 = getattr(frame, "m1", ())
    quote = getattr(frame, "quote", None)
    now = getattr(quote, "timestamp", None)
    # [Codex | 2026-09-27] 保留上游品質封鎖，不能只靠根數重新宣告 COMPLETE。
    quality = getattr(frame, "quality", None)
    if (now is None or not h1 or not m5 or not m1 or not quote.valid()
            or getattr(quality, "data_complete", False) is not True):
        return ()
    return candidate_signals(h1, m5, m1, now, version=version)
