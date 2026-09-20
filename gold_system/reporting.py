"""離線稽核摘要：不是券商成交帳本，未知值絕不補零。"""
import json
import sqlite3
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("REPORT_TIME_REQUIRES_OFFSET")
    return parsed


def summarize(database, start, end, mode, account_hash=None):
    start, end = timestamp(start), timestamp(end)
    if start >= end or mode not in ("PAPER", "DEMO"):
        raise ValueError("REPORT_INVALID_WINDOW_OR_MODE")
    # mode=ro 避免路徑打錯時建立空資料庫，也不改動交易程序狀態。
    path = Path(database).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    counts = Counter()
    pnl, segments, skipped = Decimal(0), 0, 0
    accounting = None
    try:
        for recorded, kind, raw in db.execute("SELECT time,kind,payload FROM events ORDER BY id"):
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("REPORT_INVALID_PAYLOAD")
            simulated = payload.get("simulated_at")
            # 混合資料庫不把模擬成交灌進 Demo，也不把現實記錄時間冒充回放時間。
            if (mode == "PAPER" and simulated is None) or (mode == "DEMO" and simulated is not None):
                skipped += 1
                continue
            event_time = timestamp(simulated if mode == "PAPER" else recorded)
            if not start <= event_time < end:
                continue
            # 不回顯未知事件名稱或 payload，避免自由文字帶出憑證／識別資料。
            label = kind if kind in {
                "ENTRY", "ADD_ON", "EXIT", "NO_TRADE", "RECOVERY_LOCKED",
                "DATA_INVALID", "BROKER_RECONCILIATION", "OPERATOR_STOP_NEW",
            } else "OTHER"
            counts[label] += 1
            if mode == "PAPER" and kind == "EXIT":
                value = Decimal(str(payload["pnl"]))
                if not value.is_finite():
                    raise ValueError("REPORT_INVALID_PNL")
                pnl += value
                segments += 1
        if account_hash is not None:
            from .accounting import AccountingLedger
            try:
                receipt = AccountingLedger(db, initialize=False).verified_period(
                    mode=mode, account_hash=account_hash, start=start.isoformat(), end=end.isoformat())
                # 不輸出帳戶／成交指紋，僅回報已對帳區間的金額及分段數。
                accounting = dict(status="MATCHED", segment_count=receipt["segment_count"],
                                  realized_net_pnl=receipt["net_pnl"], extra_broker_fees=receipt["extra_fees"],
                                  opening_equity=receipt["opening_equity"])
            except Exception:
                accounting = dict(status="UNVERIFIED", realized_net_pnl=None)
    finally:
        db.close()
    return {
        "status": "PROVISIONAL", "mode_declared": mode,
        "start_inclusive": start.isoformat(), "end_exclusive": end.isoformat(),
        "time_basis": "simulated_at" if mode == "PAPER" else "event_recorded_at_not_fill_time",
        "event_counts": dict(sorted(counts.items())),
        "event_counts_scope": "database_wide_not_account_filtered",
        "accounting": accounting,
        "skipped_other_time_basis_rows": skipped,
        "paper_exit_segments": segments if mode == "PAPER" else None,
        "paper_recorded_exit_pnl": str(pnl) if mode == "PAPER" and segments else None,
        "realized_net_pnl": None, "completed_trades": None, "win_rate": None,
        "max_floating_loss": None, "costs": None, "slippage": None,
        "average_r": None, "profit_factor": None, "ai_usage_and_cost": None,
        "remaining_positions": None, "working_orders": None,
        "versions_and_attribution": None,
        "qualified": False,
    }


def render(report):
    """固定模板，不呼叫模型；不把平倉分段數稱為完成交易數。"""
    lines = ["# 本機交易稽核暫結", "",
             f"模式（操作者指定）：{report['mode_declared']}",
             f"範圍：[{report['start_inclusive']}, {report['end_exclusive']})",
             f"時間依據：{report['time_basis']}", "",
             "僅摘要目前資料庫紀錄；不代表完整成交帳本或帳戶已清空。", "",
             "| 項目 | 結果 |", "|---|---|"]
    for key, value in report.items():
        if key in {"status", "mode_declared", "start_inclusive", "end_exclusive", "time_basis", "qualified"}:
            continue
        shown = "待核對／未量測" if value is None else json.dumps(value, ensure_ascii=False)
        lines.append(f"| {key} | {shown} |")
    lines += ["", "Paper 分段損益包含模擬成交價的價差／滑價影響，但不代表費用完整的淨損益。",
              "Demo 事件記錄時間不是成交時間；尚未接入日終帳務、策略歸因及通知排程。"]
    return "\n".join(lines) + "\n"


def write_report(database, start, end, mode, output, account_hash=None):
    report = summarize(database, start, end, mode, account_hash=account_hash)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    # x 模式保留既有報告，避免覆寫先前核對證據。
    with target.open("x", encoding="utf-8") as stream:
        stream.write(render(report))
    return {"output": str(target.resolve()), "status": "PROVISIONAL", "broker_access": False}
