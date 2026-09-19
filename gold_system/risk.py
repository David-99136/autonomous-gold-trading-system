"""集中處理每一筆新風險；任何未知品質狀態預設拒單。"""
from decimal import ROUND_FLOOR
from datetime import datetime
from dataclasses import replace
from .core import D, Direction, Mode, Plan, Rejected


def validate_signal(signal, policy, now):
    """進場及訊號型退出共用基本驗證；退出不需要重新取得新倉預算。"""
    if signal.direction == Direction.NO_TRADE:
        raise Rejected("NO_TRADE")
    if (not isinstance(signal.direction, Direction) or not isinstance(signal.mode, Mode)
            or not isinstance(signal.signal_id, str) or not signal.signal_id):
        raise Rejected("INVALID_SIGNAL")
    if signal.version != policy.version:
        raise Rejected("VERSION_MISMATCH")
    times = (signal.created, signal.expires, now)
    if not all(isinstance(t, datetime) and t.tzinfo is not None and t.utcoffset() is not None for t in times):
        raise Rejected("NAIVE_TIMESTAMP")
    if not signal.created <= now < signal.expires:
        raise Rejected("SIGNAL_EXPIRED_OR_FUTURE")
    if not all(isinstance(v, D) and v.is_finite() and v > 0 for v in (signal.stop, signal.target)):
        raise Rejected("INVALID_PRICES")


def make_plan(signal, quote, quality, contract, policy, equity, now, *, require_half=True):
    def require(condition, reason):
        if not condition:
            raise Rejected(reason)

    validate_signal(signal, policy, now)
    require(quote.valid(), "INVALID_QUOTE")
    require(0 <= (now - quote.timestamp).total_seconds() <= 2, "STALE_QUOTE")
    require(quality.tradeable and quality.liquid_window, "MARKET_UNAVAILABLE")
    require(quality.data_complete and quality.analysis_available, "DATA_UNAVAILABLE")
    require(quality.budget_available, "BUDGET_EXHAUSTED")
    require(abs(quality.clock_offset_ms) <= 1000, "CLOCK_DRIFT")
    require(quality.minutes_to_boundary > 30, "ENTRY_CUTOFF")
    require(not quality.extreme_volatility, "EXTREME_VOLATILITY")
    require(quality.hourly_range >= 5 or quality.trend_exception, "LOW_RANGE")
    require(quality.spread_limit.is_finite() and 0 <= quote.spread <= quality.spread_limit,
            "SPREAD_EXCEEDED")
    require(equity.is_finite() and equity > 0, "INVALID_EQUITY")
    require(signal.stop.is_finite() and signal.target.is_finite(), "INVALID_PRICES")
    entry = quote.entry(signal.direction)
    sign = signal.direction.sign
    distance = (entry - signal.stop) * sign
    # bid/ask 已經反映在 entry/exit；滑價另計，不能再次扣除 spread。
    require((quote.exit(signal.direction) - signal.stop) * sign >= contract.minimum_stop,
            "INVALID_STOP")
    # 模擬券商在進、出場各施加一次不利滑價，預算也必須涵蓋兩次。
    loss_per_unit = (distance + 2 * policy.slippage) * contract.value_per_point
    reward = (signal.target - entry) * sign - 2 * policy.slippage
    require(loss_per_unit > 0 and reward / (distance + 2 * policy.slippage) >= policy.minimum_rr,
            "REWARD_RISK_TOO_LOW")
    risk_budget = equity * policy.risk_fraction
    margin_per_unit = (entry + policy.slippage) * contract.value_per_point * contract.margin_rate
    raw = min(risk_budget / loss_per_unit,
              equity * policy.normal_margin / margin_per_unit, contract.maximum)
    size = (raw / contract.increment).to_integral_value(rounding=ROUND_FLOOR) * contract.increment
    # 50% 部分停利必須可整除兩份最小交易量，否則本策略無法執行。
    divisor = 2 if require_half else 1
    half_steps = (size / (divisor * contract.increment)).to_integral_value(rounding=ROUND_FLOOR)
    size = half_steps * divisor * contract.increment
    require(size / divisor >= contract.minimum, "MINIMUM_SIZE_EXCEEDS_BUDGET")
    return Plan(signal, entry, size, size * loss_per_unit, size * margin_per_unit)


def make_add_plan(signal, quote, quality, contract, policy, equity, now, position, confirmed_bar):
    """盈利、原部位淨保本、完成 5m 再確認後，至多加碼一次。"""
    p = position
    if p.added or signal.direction != p.direction or signal.mode != p.mode:
        raise Rejected("ADD_ON_MODE_OR_COUNT")
    if (confirmed_bar is None or not confirmed_bar.valid()
            or not p.opened_at < confirmed_bar.timestamp <= signal.created <= now
            or not 0 <= (now - confirmed_bar.timestamp).total_seconds() < 300
            or confirmed_bar.timestamp.second or confirmed_bar.timestamp.microsecond
            or confirmed_bar.timestamp.minute % 5):
        raise Rejected("ADD_ON_NEEDS_NEW_CLOSED_M5")
    sign = p.direction.sign
    if (confirmed_bar.close - confirmed_bar.open) * sign <= 0:
        raise Rejected("ADD_ON_M5_DIRECTION_UNCONFIRMED")
    if ((quote.exit(p.direction) - p.entry) * sign <= policy.slippage
            or (p.stop - p.entry) * sign < policy.slippage):
        raise Rejected("ADD_ON_NOT_PROFITABLE_AND_PROTECTED")
    if (signal.stop - p.stop) * sign < 0:
        raise Rejected("ADD_ON_STOP_CANNOT_LOOSEN")
    plan = make_plan(signal, quote, quality, contract, policy, equity, now,
                     require_half=not p.partial_taken)
    remaining_risk = max(D(0), (p.entry - signal.stop) * sign + policy.slippage) * p.size * p.value_per_point
    margin_unit = (plan.entry + policy.slippage) * p.value_per_point * contract.margin_rate
    risk_unit = plan.risk / plan.size
    raw = min(plan.size, (p.original_risk - remaining_risk) / risk_unit,
              equity * policy.normal_margin / margin_unit - p.size, contract.maximum - p.size)
    step = contract.increment * (1 if p.partial_taken else 2)
    size = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if size < contract.minimum * (1 if p.partial_taken else 2):
        raise Rejected("ADD_ON_AGGREGATE_BUDGET")
    return replace(plan, size=size, risk=size * risk_unit, margin=size * margin_unit)
