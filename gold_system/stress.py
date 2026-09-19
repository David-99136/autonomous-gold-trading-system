"""可重現的交易路徑 moving-block 壓力重抽樣；不是自動驗收或盈利預測。"""
import json
import random
from hashlib import sha256
from math import ceil

from .core import D


def stress_paths(paths, *, simulations=1000, block_size=5, seed=0, extra_cost_fraction=D(0)):
    """每個 path 是相對該筆交易起始權益的累積報酬，包含盤中浮損與最後淨損益。

    重抽樣連續交易區塊以保留區塊內連續虧損；不是把有利的平倉損益隨機拆開。
    extra_cost_fraction 僅代表額外壓力成本，不重扣原路徑已包含的價差／滑價。
    """
    if (type(simulations) is not int or not 1 <= simulations <= 10000
            or type(block_size) is not int or not paths or not 1 <= block_size <= len(paths)
            or type(seed) is not int or not isinstance(extra_cost_fraction, D)
            or not extra_cost_fraction.is_finite() or not 0 <= extra_cost_fraction <= 1):
        raise ValueError("Invalid stress configuration or insufficient paths")
    for path in paths:
        if (len(path) < 2 or path[0] != 0
                or any(not isinstance(x, D) or not x.is_finite() for x in path)):
            raise ValueError("Need complete finite normalized mark-to-market paths starting at zero")
    encoded = json.dumps([[str(x) for x in path] for path in paths], separators=(",", ":"))
    rng, drawdowns, ruin_count = random.Random(seed), [], 0
    count = len(paths)
    for _ in range(simulations):
        equity, peak, worst = D(1), D(1), D(0)
        used, ruined = 0, False
        while used < count and not ruined:
            start = rng.randrange(count-block_size+1)
            for path in paths[start:start+min(block_size, count-used)]:
                base = equity
                for change in path:
                    equity = base * (1+change-extra_cost_fraction)
                    peak = max(peak, equity)
                    worst = max(worst, (peak-equity)/peak)
                    if equity <= 0:
                        ruined = True
                        break
                used += 1
                if ruined:
                    break
        drawdowns.append(worst)
        ruin_count += ruined
    drawdowns.sort()
    return {"method": "MOVING_BLOCK_MTM_PATHS_V1", "seed": seed, "simulations": simulations,
            "source_trades": count, "block_size": block_size,
            "extra_cost_fraction": extra_cost_fraction,
            "path_sha256": sha256(encoded.encode()).hexdigest(),
            "maximum_drawdown_p95": drawdowns[ceil(simulations*.95)-1],
            "maximum_drawdown_worst": drawdowns[-1], "ruin_count": ruin_count,
            "qualified": False,
            "limitations": ["CONDITIONAL_ON_OBSERVED_PATHS", "NOT_WALK_FORWARD_VALIDATION",
                            "NOT_COMPLETE_OPERATIONAL_FAULT_STRESS", "NO_AUTOMATIC_DEMO_OR_LIVE_UNLOCK"]}
