"""以完整交易及逐時點權益評估門檻；不足資料不產生通過結論。"""
from .core import D


def performance(trade_pnl, equity_curve):
    if not equity_curve or any(not x.is_finite() for x in equity_curve) or equity_curve[0] <= 0:
        raise ValueError("Need a valid mark-to-market equity curve")
    if any(not x.is_finite() for x in trade_pnl):
        raise ValueError("Invalid realized P&L")
    gains = sum((max(x, D(0)) for x in trade_pnl), D(0))
    losses = -sum((min(x, D(0)) for x in trade_pnl), D(0))
    peak, drawdown = equity_curve[0], D(0)
    for equity in equity_curve:
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    return {"trades": len(trade_pnl), "net_pnl": sum(trade_pnl, D(0)),
            "profit_factor": gains / losses if losses else None,
            "maximum_drawdown": drawdown}


def qualification(metrics_by_mode, stress_drawdown_95, demo_days, demo_counts):
    """不將合成資料的漂亮績效轉換為 Live 解鎖；此函數只列數值門檻。"""
    reasons = []
    for mode in ("LEFT_REVERSAL", "RIGHT_CONTINUATION"):
        m = metrics_by_mode.get(mode)
        if not m or m["trades"] < 300:
            reasons.append(f"{mode}:INSUFFICIENT_OOS")
        if not m or m["profit_factor"] is None or m["profit_factor"] < D("1.30"):
            reasons.append(f"{mode}:PF_UNPROVEN")
        if not m or m["maximum_drawdown"] >= D("0.10"):
            reasons.append(f"{mode}:DRAWDOWN")
        if demo_counts.get(mode, 0) < 100:
            reasons.append(f"{mode}:INSUFFICIENT_DEMO")
    if stress_drawdown_95 is None or stress_drawdown_95 > D("0.15"):
        reasons.append("STRESS_UNPROVEN")
    if demo_days < 30:
        reasons.append("DEMO_DAYS")
    return {"numeric_gates_passed": not reasons, "reasons": reasons, "live_unlocked": False}


def operating_profit(realized_after_execution_costs, broker_fees, ai_cost, data_cost, hosting_cost):
    # 從真實成交計算的 P&L 已含價差／滑價，這裡只扣尚未記帳的費用。
    return realized_after_execution_costs - broker_fees - ai_cost - data_cost - hosting_cost
