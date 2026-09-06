"""集中處理每一筆新風險；任何未知品質狀態預設拒單。"""
from decimal import ROUND_FLOOR
from .core import D, Direction, Plan, Rejected


def make_plan(signal, quote, quality, contract, policy, equity, now):
    def require(condition, reason):
        if not condition:
            raise Rejected(reason)

    require(signal.direction != Direction.NO_TRADE, "NO_TRADE")
    require(signal.version == policy.version, "VERSION_MISMATCH")
    require(signal.created.tzinfo is not None and signal.expires.tzinfo is not None
            and now.tzinfo is not None, "NAIVE_TIMESTAMP")
    require(signal.created <= now < signal.expires, "SIGNAL_EXPIRED_OR_FUTURE")
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
    loss_per_unit = (distance + policy.slippage) * contract.value_per_point
    reward = (signal.target - entry) * sign - policy.slippage
    require(loss_per_unit > 0 and reward / (distance + policy.slippage) >= policy.minimum_rr,
            "REWARD_RISK_TOO_LOW")
    risk_budget = equity * policy.risk_fraction
    margin_per_unit = entry * contract.value_per_point * contract.margin_rate
    raw = min(risk_budget / loss_per_unit,
              equity * policy.normal_margin / margin_per_unit, contract.maximum)
    size = (raw / contract.increment).to_integral_value(rounding=ROUND_FLOOR) * contract.increment
    # 50% 部分停利必須可整除兩份最小交易量，否則本策略無法執行。
    half_steps = (size / (2 * contract.increment)).to_integral_value(rounding=ROUND_FLOOR)
    size = half_steps * 2 * contract.increment
    require(size / 2 >= contract.minimum, "MINIMUM_SIZE_EXCEEDS_BUDGET")
    return Plan(signal, entry, size, size * loss_per_unit, size * margin_per_unit)
