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
        signal = Signal("synthetic-001", policy.version, start, start + timedelta(minutes=1),
                        Direction.LONG, Mode.RIGHT, D(1990), D(2025), "SYNTHETIC_TEST_ONLY")
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
    sub.add_parser("credentials-set", help="Interactively save Demo credentials to Windows vault")
    discovery = sub.add_parser("demo-discover", help="Demo login and read market data only")
    discovery.add_argument("--epic", help="Inspect a previously discovered epic")
    discovery.add_argument("--output", default="runtime/discovery.json")
    args = parser.parse_args()
    if args.command == "replay":
        asyncio.run(replay(args.output))
    elif args.command == "replay-csv":
        from .replay import replay_csv
        report = asyncio.run(replay_csv(args.csv, args.contract, args.output))
        print(json.dumps({"metrics": report["metrics"], "qualified": False}, default=str))
    elif args.command == "credentials-set":
        from .capital import save_credentials
        save_credentials()
        print("Demo credentials saved to Windows Credential Manager.")
    else:
        from .capital import CapitalDemo, load_credentials
        client = CapitalDemo()
        try:
            client.login(load_credentials())
            data = client.market(args.epic) if args.epic else client.discover()
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Demo market metadata saved: {path.resolve()}")
        finally:
            client.close()


if __name__ == "__main__":
    main()
