"""可直接執行的離線展示、日誌報告與 Demo 商品探測指令。"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .core import D, Contract, Direction, Mode, Quote, Quality, RiskPolicy, Signal
from .broker import PaperBroker
from .engine import Engine
from .store import Store


async def replay(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    db = output / "events.db"
    if db.exists():
        raise ValueError("Output contains an existing run; choose a new --output directory")
    store = Store(db)
    try:
        policy = RiskPolicy()
        contract = Contract("SYNTHETIC_GOLD", D(1), D("0.01"), D("0.01"), D(100), D("0.01"), D("0.1"))
        broker = PaperBroker()
        engine = Engine(store, broker, contract, policy)
        start = datetime(2026, 1, 5, 14, tzinfo=timezone.utc)
        quality = Quality(True, True, True, False, D("0.6"), 0, D(120), D(12), False, True, True)
        signal = Signal(
            signal_id="synthetic-001", version=policy.version, created=start,
            expires=start + timedelta(minutes=1), direction=Direction.LONG,
            mode=Mode.RIGHT, stop=D(1990), target=D(2025), reason="SYNTHETIC_TEST_ONLY",
            # 新欄位使用預設値（合成資料，不代表真實市場分析）
            # [Codex | 2026-09-27] 新倉契約已收緊，僅為合成示範明確填值。
            zone_id="SYNTHETIC_ONLY", confirmation_condition="SYNTHETIC_ONLY",
            invalidation_price=D(1990), data_completeness="COMPLETE",
        )
        for i, bid in enumerate(("2000", "2012", "2015", "2010")):
            now = start + timedelta(minutes=i)
            await engine.tick(now, Quote(now, D(bid), D(bid) + D("0.5")), quality,
                              signal if i == 0 else None, D(2011) if i == 2 else None)
        result = {"data": "SYNTHETIC — not market data or backtest evidence", "mode": "OFFLINE_PAPER",
                  "balance": str(broker.balance), "events": store.events(),
                  "validation": "NOT_QUALIFIED_FOR_DEMO_OR_LIVE"}
        (output / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        lines = ["# 離線合成資料功能展示", "", "此結果僅验证程式流程，不代表策略報酬。", "",
                 f"期末模擬餘額：{broker.balance}", "", "| 事件 | 內容 |", "|---|---|"]
        lines += [f"| {e['kind']} | {json.dumps(e['payload'], ensure_ascii=False)} |" for e in store.events()]
        (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
        print(json.dumps({"output": str(output.resolve()), "balance": str(broker.balance),
                          "source": "SYNTHETIC", "qualified": False}, ensure_ascii=False))
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Gold CFD offline prototype / Capital Demo discovery")
    sub = parser.add_subparsers(dest="command", required=True)
    replay_parser = sub.add_parser("replay", help="Offline synthetic execution demonstration")
    replay_parser.add_argument("--output", default="runtime/replay")
    csv_parser = sub.add_parser("replay-csv", help="Bid/ask OHLC research replay; not Live qualification")
    csv_parser.add_argument("--csv", required=True)
    csv_parser.add_argument("--contract", required=True)
    csv_parser.add_argument("--output", required=True)
    walk = sub.add_parser("walk-forward-csv", help="Frozen price-prototype OOS replay; no AI training or qualification")
    walk.add_argument("--csv", required=True)
    walk.add_argument("--contract", required=True)
    walk.add_argument("--output", required=True)
    walk.add_argument("--train-size", type=int, required=True)
    walk.add_argument("--test-size", type=int, required=True)
    walk.add_argument("--purge-size", type=int, default=0)
    walk.add_argument("--initial-equity", required=True)
    sub.add_parser("credentials-set", help="Interactively save Demo credentials to Windows vault")
    codex_check = sub.add_parser("codex-check", help="One Codex subscription call with synthetic NO_TRADE input; no broker access")
    codex_check.add_argument("--model", help="Optional explicit Codex model; omitted uses CLI default")
    stream = sub.add_parser("demo-stream", help="Read-only GOLD WebSocket probe; never places orders")
    stream.add_argument("--seconds", type=int, default=15)
    stream.add_argument("--epic", choices=("GOLD", "ETHUSD"), default="GOLD")
    stream.add_argument("--output", required=True, help="New local JSONL file; excludes credentials")
    discovery = sub.add_parser("demo-discover", help="Demo login and read market data only")
    discovery.add_argument("--epic", help="Inspect a previously discovered epic")
    discovery.add_argument("--output", default="runtime/discovery.json")
    capture = sub.add_parser("demo-history", help="Download raw Demo GOLD minute history; no orders")
    capture.add_argument("--start", required=True, help="ISO timestamp with offset")
    capture.add_argument("--end", required=True, help="ISO timestamp with offset")
    capture.add_argument("--output", required=True)
    audit = sub.add_parser("history-audit", help="Offline history checksums and reconstruction; no broker calls")
    audit.add_argument("--directory", required=True)
    # [Antigravity | 2026-09-27] 離線 raw JSON 歷史品質核對入口（A 批）
    history_check = sub.add_parser("history-check", help="Offline raw GOLD file quality check; never unlocks trading")
    history_check.add_argument("--input", required=True, help="Local raw response JSON, at most 2 MiB")
    history_check.add_argument("--database", required=True, help="Existing Store; no implicit creation")
    history_check.add_argument("--create-diagnostic", action="store_true", help="Explicitly create a NEW diagnostic database")
    history_check.add_argument("--start", required=True, help="Inclusive raw timestamp with UTC offset")
    history_check.add_argument("--end", required=True, help="Inclusive raw timestamp with UTC offset")
    history_check.add_argument("--received-at", required=True, help="Caller-supplied receipt timestamp with UTC offset")
    # [Antigravity | 2026-09-27] Demo 唯讀歷史採樣器計畫核對與受限執行入口
    sample_plan = sub.add_parser("history-sample-plan", help="Validate local sampling plan and print canonical hash; offline")
    sample_plan.add_argument("--plan", required=True)
    sample_run = sub.add_parser("demo-history-sample", help="Explicit bounded Demo read-only evidence capture; no orders")
    sample_run.add_argument("--plan", required=True)
    sample_run.add_argument("--plan-sha256", required=True)
    sample_run.add_argument("--output-name", required=True, help="New directory name under project runtime")
    sample_run.add_argument("--confirm-demo-read-only", action="store_true")
    sample_run.add_argument("--exclusive-account", action="store_true", help="Confirm no other API clients use this account")
    reconcile = sub.add_parser("demo-reconcile", help="Read-only Demo exposure inspection; never unlocks trading")
    reconcile.add_argument("--database", default="runtime/demo-audit.db")
    reconcile.add_argument("--async-http", action="store_true", help="Use pooled async Demo reads; writes remain disabled")
    costs = sub.add_parser("cost-status", help="Local USD budget status; no provider calls or budget changes")
    costs.add_argument("--database", default="runtime/demo-audit.db")
    stop = sub.add_parser("stop-new", help="Persistently block new entries; does not flatten or cancel orders")
    stop.add_argument("--database", required=True, help="Existing engine/gateway audit database")
    stress = sub.add_parser("stress-replay", help="Offline MTM path stress from replay report; not qualification")
    stress.add_argument("--report", required=True)
    stress.add_argument("--output", required=True)
    stress.add_argument("--simulations", type=int, default=1000)
    stress.add_argument("--block-size", type=int, default=5)
    stress.add_argument("--seed", type=int, default=0)
    stress.add_argument("--extra-cost-fraction", default="0")
    daily = sub.add_parser("daily-report", help="Offline provisional audit summary; no broker/model calls")
    daily.add_argument("--database", required=True)
    daily.add_argument("--start", required=True, help="Inclusive ISO timestamp with UTC offset")
    daily.add_argument("--end", required=True, help="Exclusive ISO timestamp with UTC offset")
    daily.add_argument("--mode", required=True, choices=("PAPER", "DEMO"))
    daily.add_argument("--account-hash", help="Optional account SHA-256 for verified settlement totals; never a raw account ID")
    daily.add_argument("--output", required=True, help="New Markdown file; never overwrite")
    args = parser.parse_args()
    if args.command in ("history-sample-plan", "demo-history-sample"):
        try:
            if args.command == "history-sample-plan":
                from .sampler_plan import read_plan, digest
                plan = read_plan(args.plan)
                result = dict(status="PLAN_VALID", plan_sha256=digest(plan), queries=len(plan["queries"]),
                              start=plan["start"], end=plan["end"], entries_enabled=False, qualified=False)
                code = 0
            else:
                from .sampler_cli import run_demo
                result = asyncio.run(run_demo(args.plan, args.plan_sha256, args.output_name,
                    confirmed=args.confirm_demo_read_only, exclusive_account=args.exclusive_account))
                code = 0 if result["status"] == "COMPLETED_UNVERIFIED" else 1
        except Exception:
            # 不直接輸出 vault、檔案路徑、HTTP 或外部 JSON 的例外原文。
            result, code = dict(status="FAILED", reason="SAMPLER_COMMAND_FAILED",
                                entries_enabled=False, closure_verified=False, qualified=False), 1
        print(json.dumps(result))
        raise SystemExit(code)
    elif args.command == "replay":
        asyncio.run(replay(args.output))
    elif args.command == "daily-report":
        from .reporting import write_report
        try:
            result = write_report(args.database, args.start, args.end, args.mode, args.output, account_hash=args.account_hash)
        except Exception:
            # 壞資料的例外可能含原始 payload，對使用者只顯示固定錯誤。
            parser.error("REPORT_FAILED: check database, offset-aware window and unused output path")
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "demo-stream":
        if not 1 <= args.seconds <= 300:
            parser.error("seconds must be between 1 and 300")
        from .stream_probe import capture_quotes
        print(json.dumps(asyncio.run(capture_quotes(args.output, args.seconds, epic=args.epic))))
    elif args.command == "codex-check":
        from .codex_analysis import CodexAnalysis
        report = asyncio.run(CodexAnalysis(model=args.model).select([], {"evidence_eligible": False,
            "records": {}, "purpose": "SYNTHETIC_CONNECTIVITY_CHECK_NO_MARKET_DATA"}))
        print(json.dumps(report, ensure_ascii=False))
    elif args.command == "history-audit":
        from .history import audit_download
        print(json.dumps(audit_download(args.directory), ensure_ascii=False))
    elif args.command == "history-check":
        from .history_import import audit_file
        result, code = audit_file(args.database, args.input, start=args.start, end=args.end,
                                  received_at=args.received_at, create_diagnostic=args.create_diagnostic)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(code)
    elif args.command == "stop-new":
        if not Path(args.database).is_file():
            parser.error("stop-new requires an existing database; no database was created")
        store = Store(args.database)
        try:
            store.stop_new_entries()
            print(json.dumps({"database": str(Path(args.database).resolve()), "new_entries_stopped": True,
                              "broker_flattened": False, "broker_orders_cancelled": False}))
        finally:
            store.close()
    elif args.command == "replay-csv":
        from .replay import replay_csv
        report = asyncio.run(replay_csv(args.csv, args.contract, args.output))
        print(json.dumps({"metrics": report["metrics"], "qualified": False}, default=str))
    elif args.command == "walk-forward-csv":
        from .replay import read_candles
        from .walk_forward import run_walk_forward, replay_evaluator
        data = json.loads(Path(args.contract).read_text(encoding="utf-8"))
        contract = Contract(data["epic"], **{k: D(str(v)) for k, v in data.items() if k != "epic"})
        root = Path(args.output)
        if root.exists():
            parser.error("walk-forward-csv requires a new output directory")
        config = {"strategy_version": RiskPolicy().version, "model_version": "NOT_USED", "prompt_version": "NOT_USED"}
        # 固定版本基準，不根據訓練或測試的收益挑參數；不能稱為已訓練 AI。
        result = run_walk_forward(read_candles(args.csv), lambda _: dict(config),
            replay_evaluator(contract, root/"replays"), root/"walk-forward",
            train_size=args.train_size, test_size=args.test_size, purge_size=args.purge_size,
            initial_equity=D(args.initial_equity))
        print(json.dumps({"output": str(root.resolve()), "metrics": result["global_oos"]["metrics"],
                          "qualified": False, "strategy": "FROZEN_PRICE_PROTOTYPE_NO_AI"}, default=str))
    elif args.command == "stress-replay":
        from .stress import stress_paths
        from hashlib import sha256
        raw = Path(args.report).read_bytes()
        source = json.loads(raw)
        paths = [[D(x) for x in trade["returns"]] for trade in source["trade_paths"]]
        result = stress_paths(paths, simulations=args.simulations, block_size=args.block_size,
                              seed=args.seed, extra_cost_fraction=D(args.extra_cost_fraction))
        result["source_report_sha256"] = sha256(raw).hexdigest()
        result["source_type"] = source.get("type", "UNAVAILABLE")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, default=str, indent=2)
        print(json.dumps({"output": str(output.resolve()), "qualified": False}))
    elif args.command == "credentials-set":
        from .capital import save_credentials
        save_credentials()
        print("Demo credentials saved to Windows Credential Manager.")
    elif args.command == "cost-status":
        from .costs import CostLedger
        store = Store(args.database)
        try:
            print(json.dumps(CostLedger(store).status(datetime.now(timezone.utc)), default=str))
        finally:
            store.close()
    else:
        from .capital import CapitalDemo, load_credentials
        if args.command == "demo-reconcile" and args.async_http:
            from .async_capital import AsyncCapitalDemo
            from .reconciliation import inspect_demo_async
            async def inspect():
                client = AsyncCapitalDemo()  # 沒有 write_guard，任何訂單写入均被拒絕。
                store = Store(args.database)
                try:
                    await client.login(load_credentials())
                    print(json.dumps(await inspect_demo_async(client, store), ensure_ascii=False))
                finally:
                    await client.close()
                    store.close()
            asyncio.run(inspect())
            return
        client = CapitalDemo()
        try:
            client.login(load_credentials())
            if args.command == "demo-reconcile":
                from .reconciliation import inspect_demo
                store = Store(args.database)
                try:
                    print(json.dumps(inspect_demo(client, store), ensure_ascii=False))
                finally:
                    store.close()
                return
            if args.command == "demo-history":
                from .history import download
                result = download(client, "GOLD", datetime.fromisoformat(args.start), datetime.fromisoformat(args.end), args.output)
                print(json.dumps({"rows": result["rows"], "output": args.output, "qualified": False}))
                return
            data = client.market(args.epic) if args.epic else client.discover()
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Demo market metadata saved: {path.resolve()}")
        finally:
            client.close()


if __name__ == "__main__":
    main()
