"""時間隔離的 walk-forward 協調器；fit/evaluate 仍需接入經審查的策略實作。"""
import json
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from .core import D, Mode, Direction
from .validation import performance


def replay_evaluator(contract, output):
    """將現有無 AI 價格策略接入同步 walk-forward，拒絕尚未支援的調參設定。

    這是固定版本基準評估器，不是訓練器；不可宣稱 NOT_USED 模型做過新聞分析。
    """
    import asyncio
    from datetime import datetime
    from .core import RiskPolicy
    from .replay import replay_candles

    expected = {"strategy_version": RiskPolicy().version, "model_version": "NOT_USED", "prompt_version": "NOT_USED"}

    def evaluate(config, warmup, testing, *, initial_equity):
        if config != expected:
            raise ValueError("Replay evaluator only supports the exact frozen price-prototype configuration")
        test_hash = digest([asdict(c) for c in testing])
        report = asyncio.run(replay_candles(testing, contract, Path(output)/test_hash,
            initial_equity, warmup=warmup, input_sha256=test_hash, contract_sha256=digest(asdict(contract))))
        return {"equity_points": report["equity_points"],
                "trades": [{"opened_at": datetime.fromisoformat(t["opened_at"]),
                            "closed_at": datetime.fromisoformat(t["closed_at"]),
                            "strategy_mode": t["strategy_mode"], "direction": t["direction"],
                            "net_pnl": t["net_pnl"]} for t in report["trade_paths"]],
                "replay_type": report["type"], "replay_limitations": report["limitations"]}
    return evaluate


def canonical(value):
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def windows(count, train_size, test_size, purge_size=0):
    """連續、不重疊測試窗；尾端不足完整測試窗者明確列為未測，不補造。"""
    if (any(type(x) is not int for x in (count, train_size, test_size, purge_size))
            or count < 0 or train_size <= 0 or test_size <= 0 or purge_size < 0):
        raise ValueError("Invalid walk-forward windows")
    result = []
    cursor = train_size+purge_size
    while cursor+test_size <= count:
        result.append((cursor-purge_size-train_size, cursor-purge_size, cursor, cursor+test_size))
        cursor += test_size
    return result


def validate_result(result, start, end, required_marks=()):
    """只有完全位於測試窗內的完整交易可列 OOS；跨窗部位不能混入。"""
    points, trades = result["equity_points"], result["trades"]
    if len(points) < 2 or points[0][0] != start or points[-1][0] != end:
        raise ValueError("Need the full test-window equity path")
    if (any(not start <= t <= end or not isinstance(e, D) or not e.is_finite() for t, e in points)
            or any(a[0] >= b[0] for a, b in zip(points, points[1:]))):
        raise ValueError("Invalid or out-of-window equity observations")
    if not set(required_marks).issubset({t for t, _ in points}):
        raise ValueError("Missing required intrabar equity marks")
    previous_close = start
    for trade in trades:
        if (not start <= trade["opened_at"] < trade["closed_at"] <= end
                or trade["opened_at"] < previous_close
                or trade["strategy_mode"] not in tuple(Mode)
                or trade["direction"] not in (Direction.LONG, Direction.SHORT)
                or not isinstance(trade["net_pnl"], D) or not trade["net_pnl"].is_finite()):
            raise ValueError("Invalid, overlapping or out-of-window OOS trade")
        previous_close = trade["closed_at"]
    # 此版本不允許未歸屬的入金、提款或帳外費用污染 OOS 報酬。
    if sum((t["net_pnl"] for t in trades), D(0)) != points[-1][1]-points[0][1]:
        raise ValueError("Test equity and net trade accounting do not reconcile")
    return performance([t["net_pnl"] for t in trades], [e for _, e in points])


def run_walk_forward(candles, fit, evaluate, output, *, train_size, test_size, purge_size=0,
                     initial_equity=None):
    """fit 只收到 train；evaluate 收到設定副本、過去 warmup、test，不傳後續樣本。

    Python callback 不是安全沙箱：策略的外部 I/O、宏觀 vintage 與特徵製作仍需審查。
    因而此工具的成功只證明介面與結果邊界，不會自動授予策略驗收資格。
    """
    if initial_equity is not None and (not isinstance(initial_equity, D)
            or not initial_equity.is_finite() or initial_equity <= 0):
        raise ValueError("Initial OOS equity must be a positive Decimal")
    capital = initial_equity
    global_points, global_trades = [], []
    for i, candle in enumerate(candles):
        if (not candle.bid.valid() or not candle.ask.valid() or candle.bid.timestamp != candle.ask.timestamp
                or candle.bid.timestamp.second or candle.bid.timestamp.microsecond
                or any(getattr(candle.bid, k) > getattr(candle.ask, k) for k in ("open", "high", "low", "close"))
                or (i and candle.bid.timestamp <= candles[i-1].bid.timestamp)):
            raise ValueError("Invalid or unordered source candles")
    folds = windows(len(candles), train_size, test_size, purge_size)
    if not folds:
        raise ValueError("Insufficient history for one complete train/test fold")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    reports = []
    for index, (left, train_end, test_start, right) in enumerate(folds):
        training = tuple(candles[left:train_end])
        warmup, testing = tuple(candles[left:test_start]), tuple(candles[test_start:right])
        config = fit(training)
        if not isinstance(config, dict) or not all(isinstance(config.get(k), str) and config[k]
                for k in ("strategy_version", "model_version", "prompt_version")):
            raise ValueError("Fit must return explicit frozen strategy/model/prompt versions")
        frozen = canonical(config)
        isolated_config = json.loads(frozen)
        start, end = testing[0].bid.timestamp-timedelta(minutes=1), testing[-1].bid.timestamp
        if training[-1].bid.timestamp > start:
            raise ValueError("Training overlaps test execution time")
        record = {"fold": index, "train_range": [left, train_end], "test_range": [test_start, right],
                  "purge_size": purge_size, "test_start": start, "test_end": end,
                  "train_sha256": digest([asdict(c) for c in training]),
                  "test_sha256": digest([asdict(c) for c in testing]),
                  "config": isolated_config, "config_sha256": sha256(frozen.encode()).hexdigest(),
                  "initial_equity": capital,
                  "qualified": False}
        # 先凍結輸入與設定，再准許 evaluate 看到 test。中途失敗留下 prepare，沒有 result。
        with (root/f"fold-{index:04d}-prepared.json").open("x", encoding="utf-8") as stream:
            stream.write(canonical(record))
        if capital is None:
            result = evaluate(isolated_config, warmup, testing)
        else:
            # 實際將前窗期末資金交給 evaluator 重新定倉；不可事後比例縮放損益冒充重播。
            if capital <= 0:
                raise ValueError("OOS capital exhausted; cannot restart a bankrupt account")
            result = evaluate(isolated_config, warmup, testing, initial_equity=capital)
        if canonical(isolated_config) != frozen:
            raise ValueError("Evaluator modified the frozen configuration")
        marks = [c.bid.timestamp-timedelta(seconds=offset) for c in testing for offset in (60, 45, 30, 15)]
        record["metrics"] = validate_result(result, start, end, marks)
        if capital is not None:
            points = result["equity_points"]
            if points[0][1] != capital:
                raise ValueError("Evaluator reset or changed carried OOS capital")
            if any(e <= 0 for _, e in points):
                raise ValueError("OOS insolvency requires an explicit liquidation model")
            if global_points and points[0][0] < global_points[-1][0]:
                raise ValueError("Overlapping global OOS observations")
            global_points.extend(points[1:] if global_points and points[0][0] == global_points[-1][0] else points)
            global_trades.extend(result["trades"])
            capital = points[-1][1]
        record["result_sha256"] = digest(result)
        with (root/f"fold-{index:04d}-result.json").open("x", encoding="utf-8") as stream:
            stream.write(canonical({**record, "result": result}))
        reports.append(record)
    summary = {"method": "TIME_ORDERED_WALK_FORWARD_V2", "folds": reports,
               "source_sha256": digest([asdict(c) for c in candles]),
               "unused_tail_samples": len(candles)-folds[-1][3], "qualified": False,
               "limitations": ["CALLBACK_EXTERNAL_IO_NOT_SANDBOXED", "POINT_IN_TIME_VINTAGES_UNVERIFIED",
                               "NO_AUTOMATIC_DEMO_OR_LIVE_UNLOCK"]}
    if initial_equity is None:
        summary["limitations"].append("NO_GLOBAL_EQUITY_STITCHING")
    else:
        groups = {}
        for mode in Mode:
            for direction in (Direction.LONG, Direction.SHORT):
                pnl = [t["net_pnl"] for t in global_trades
                       if t["strategy_mode"] == mode and t["direction"] == direction]
                gains = sum((max(x, D(0)) for x in pnl), D(0))
                losses = -sum((min(x, D(0)) for x in pnl), D(0))
                groups[f"{mode}:{direction}"] = {"trades": len(pnl), "net_pnl": sum(pnl, D(0)),
                    "wins": sum(x > 0 for x in pnl), "losses": sum(x < 0 for x in pnl),
                    "breakeven": sum(x == 0 for x in pnl), "profit_factor": gains/losses if losses else None}
        summary["global_oos"] = {"capital_method": "CARRIED_TO_EVALUATOR_NO_RESCALE",
            "initial_equity": initial_equity, "final_equity": capital,
            "equity_points": global_points, "trade_groups": groups,
            "metrics": performance([t["net_pnl"] for t in global_trades], [e for _, e in global_points])}
        summary["limitations"].append("GROUP_SPECIFIC_MTM_DRAWDOWN_NOT_ATTRIBUTED")
    with (root/"summary.json").open("x", encoding="utf-8") as stream:
        stream.write(canonical(summary))
    return summary
