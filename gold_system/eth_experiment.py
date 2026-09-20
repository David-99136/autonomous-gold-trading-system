"""使用者明確啟動的 ETHUSD Demo 微量驗證，獨立於 GOLD 自動進場路徑。

固定最多 0.002 初始部位與一次 0.001 淨額減倉試驗；每次寫入先落盤。
禁止重跑同一日誌、重送未知訂單、修改帳戶設定或操作既有部位。
"""
import argparse
import asyncio
import json
import os
import time
from contextlib import aclosing
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import quote

from .async_capital import AsyncCapitalDemo
from .capital import load_credentials
from .clock_sync import synchronize


def check_market(market):
    instrument, rules, snapshot = market["instrument"], market["dealingRules"], market["snapshot"]
    if (instrument["epic"] != "ETHUSD" or instrument["currency"] != "USD"
            or instrument["type"] != "CRYPTOCURRENCIES" or not instrument["guaranteedStopAllowed"]
            or snapshot["marketStatus"] != "TRADEABLE" or snapshot["marketModes"] != ["REGULAR"]):
        raise ValueError("EXPERIMENT_MARKET_INELIGIBLE")
    for field in ("minDealSize", "minSizeIncrement"):
        if rules[field]["unit"] != "POINTS" or D(str(rules[field]["value"])) != D("0.001"):
            raise ValueError("EXPERIMENT_SIZE_CHANGED")
    minimum = rules["minGuaranteedStopDistance"]
    if minimum["unit"] != "PERCENTAGE" or D(str(minimum["value"])) > D("0.5"):
        raise ValueError("EXPERIMENT_STOP_RULE_CHANGED")


class Experiment:
    def __init__(self, client, audit, *, hold_seconds=0):
        self.client, self.audit = client, audit
        self.steps, self.owned = set(), set()
        self.account = None
        if type(hold_seconds) is not int or not 0 <= hold_seconds <= 600:
            raise ValueError("EXPERIMENT_INVALID_HOLD_SECONDS")
        self.hold_seconds = hold_seconds

    def record(self, event, **fields):
        self.audit.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(), event=event,
                                         **fields), default=str) + "\n")
        self.audit.flush()
        os.fsync(self.audit.fileno())

    async def same_account(self):
        current = await self.client.session()
        if current.get("currency") != "USD" or current.get("accountId") != self.account:
            raise ValueError("EXPERIMENT_ACCOUNT_CHANGED")

    async def write_once(self, step, method, path, payload=None):
        if step in self.steps:
            raise ValueError("EXPERIMENT_WRITE_ALREADY_ATTEMPTED")
        await self.same_account()
        if step == "OPEN" and not 0 <= (self.clock.now()-self.price.timestamp).total_seconds() <= 2:
            raise ValueError("EXPERIMENT_STALE_QUOTE")
        self.steps.add(step)
        self.record("SUBMITTING", step=step, method=method, path=path, request=payload)
        started = time.monotonic()
        response, _ = await self.client._request(method, path, payload)
        self.record("ACKNOWLEDGED", step=step, response=response,
                    elapsed_seconds=time.monotonic()-started)
        reference = response.get("dealReference")
        if not isinstance(reference, str) or not reference:
            raise ValueError("EXPERIMENT_REFERENCE_MISSING")
        result = await self.client.confirmation(reference)
        self.record("CONFIRMATION", step=step, result=result)
        # 保存開倉／反向試驗真正涉及的部位 ID，finally 只清理這些 ID。
        if step in ("OPEN", "REDUCE"):
            for affected in result.get("affectedDeals", []):
                if affected.get("status") in ("OPENED", "AMENDED"):
                    self.owned.add(affected["dealId"])
            if result.get("dealId"):
                self.owned.add(result["dealId"])
        if result.get("dealStatus") != "ACCEPTED":
            raise ValueError("EXPERIMENT_WRITE_NOT_ACCEPTED")
        return result

    async def snapshot(self, stage):
        data = await self.client.positions()
        orders = await self.client.working_orders()
        self.record("SNAPSHOT", stage=stage, positions=data["positions"], orders=orders["workingOrders"])
        return data["positions"], orders["workingOrders"]

    async def own_position(self, size):
        positions, orders = await self.snapshot("VERIFY_SIZE")
        if len(positions) != 1 or orders:
            raise ValueError("EXPERIMENT_UNEXPECTED_EXPOSURE")
        row = positions[0]
        p = row["position"]
        if (p["dealId"] not in self.owned or row["market"]["epic"] != "ETHUSD"
                or p["direction"] != "BUY" or D(str(p["size"])) != size):
            raise ValueError("EXPERIMENT_POSITION_MISMATCH")
        return p

    async def run(self):
        session = await self.client.session()
        self.account = session.get("accountId")
        if not self.account or session.get("currency") != "USD":
            raise ValueError("EXPERIMENT_ACCOUNT_INELIGIBLE")
        positions, orders = await self.snapshot("BASELINE")
        if positions or orders:
            raise ValueError("EXPERIMENT_REQUIRES_EMPTY_ACCOUNT")
        preferences = await self.client.preferences()
        if preferences.get("hedgingMode") is not False:
            raise ValueError("EXPERIMENT_REQUIRES_NETTING")
        market, _ = await self.client._request("GET", "/markets/ETHUSD")
        check_market(market)
        self.record("PREFLIGHT", market=market, crypto_leverage=preferences.get("leverages", {}).get("CRYPTOCURRENCIES"))
        clock = await synchronize(self.client)
        self.clock = clock
        self.record("CLOCK", offset_ms=clock.offset_ms, uncertainty_ms=clock.uncertainty_ms)
        async with aclosing(self.client.quotes(epic="ETHUSD", clock=clock.now)) as feed:
            price = await anext(feed)
        self.price = price
        if not D(100) <= price.bid <= price.ask <= D(3500) or price.spread > D(5):
            raise ValueError("EXPERIMENT_PRICE_ENVELOPE")
        stop = price.bid - D(20)
        target = price.ask + D(40)
        outcome = "INCOMPLETE"
        try:
            if (clock.now()-price.timestamp).total_seconds() > 2:
                raise ValueError("EXPERIMENT_STALE_QUOTE")
            await self.write_once("OPEN", "POST", "/positions", dict(epic="ETHUSD", direction="BUY",
                size=D("0.002"), guaranteedStop=True, stopLevel=stop, profitLevel=target))
            position = await self.own_position(D("0.002"))
            if not position.get("guaranteedStop") or D(str(position.get("stopLevel", 0))) < stop:
                raise ValueError("EXPERIMENT_STOP_MISSING")
            deal = position["dealId"]
            if self.hold_seconds:
                # 單次使用者授權的 Demo 持倉：不建立掛單，到期後 finally 只清理本次 deal。
                self.record("HOLD_STARTED", hold_seconds=self.hold_seconds)
                await asyncio.sleep(self.hold_seconds)
                self.record("HOLD_EXPIRED", hold_seconds=self.hold_seconds)
                outcome = "HOLD_WINDOW_COMPLETED"
            else:
                await self.write_once("TIGHTEN", "PUT", "/positions/"+quote(deal, safe=""),
                                      dict(guaranteedStop=True, stopLevel=stop+D(1), profitLevel=target))
                position = await self.own_position(D("0.002"))
                if D(str(position.get("stopLevel", 0))) < stop+D(1):
                    raise ValueError("EXPERIMENT_STOP_NOT_TIGHTENED")
                # DELETE 僅有全平接口；反向 0.001 的淨額效果在此實驗確認。
                if (await self.client.preferences()).get("hedgingMode") is not False:
                    raise ValueError("EXPERIMENT_NETTING_CHANGED")
                await self.write_once("REDUCE", "POST", "/positions", dict(epic="ETHUSD", direction="SELL",
                    size=D("0.001"), guaranteedStop=True, stopDistance=D(20)))
                position = await self.own_position(D("0.001"))
                self.record("PARTIAL_CLOSE_VERIFIED", remaining_size=position["size"],
                            stopLevel=position.get("stopLevel"), guaranteedStop=position.get("guaranteedStop"))
                outcome = "LIFECYCLE_VERIFIED"
        except Exception as exc:
            self.record("EXPERIMENT_INTERRUPTED", error_type=type(exc).__name__)
        finally:
            # 清理基於最新 snapshot，不對未知開倉重送；若無法識別，保留審計並回報。
            positions, _ = await self.snapshot("BEFORE_CLEANUP")
            for row in positions:
                deal = row["position"]["dealId"]
                if deal in self.owned and row["market"]["epic"] == "ETHUSD":
                    try:
                        await self.write_once("CLOSE_"+deal, "DELETE", "/positions/"+quote(deal, safe=""))
                    except Exception as exc:
                        self.record("CLEANUP_UNCERTAIN", error_type=type(exc).__name__)
            positions, orders = await self.snapshot("FINAL")
            clean = not positions and not orders
            self.record("RESULT", outcome=outcome, clean=clean,
                        position_count=len(positions), working_order_count=len(orders))
            if not clean:
                raise RuntimeError("EXPERIMENT_EXPOSURE_REQUIRES_RECONCILIATION")
        return dict(outcome=outcome, clean=clean)


async def execute(path, *, hold_seconds=0):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as audit:
        client = AsyncCapitalDemo()
        try:
            await client.login(load_credentials())
            return await Experiment(client, audit, hold_seconds=hold_seconds).run()
        finally:
            await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One bounded ETHUSD Demo experiment; no strategy trading")
    parser.add_argument("--execute-demo-experiment", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--hold-seconds", type=int, default=0,
                        help="Authorized Demo hold window; zero runs the lifecycle validation.")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(execute(args.output, hold_seconds=args.hold_seconds))))
