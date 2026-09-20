"""唯讀 Demo 帳務證據擷取；原始業務資料僅存本機，不直接入帳。"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

from .reporting import timestamp


def history_query(start, end):
    start, end = timestamp(start), timestamp(end)
    if not 0 < (end - start).total_seconds() <= 86400:
        raise ValueError("HISTORY_WINDOW_INVALID")
    return urlencode({"from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                      "to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")})


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if not any(x in k.lower() for x in ("token", "password", "secret", "api_key", "apikey", "cst"))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


async def collect_history(client, start, end):
    query = history_query(start, end)
    before = await client.session()
    transactions, _ = await client._request("GET", "/history/transactions?" + query)
    activity, _ = await client._request("GET", "/history/activity?" + query + "&detailed=true")
    after = await client.session()
    account = before.get("accountId")
    if not isinstance(account, str) or not account or account != after.get("accountId"):
        raise ValueError("HISTORY_ACCOUNT_CHANGED")
    if before.get("currency") != "USD" or after.get("currency") != "USD":
        raise ValueError("HISTORY_ACCOUNT_CURRENCY_UNVERIFIED")
    if not isinstance(transactions.get("transactions"), list) or not isinstance(activity.get("activities"), list):
        raise ValueError("HISTORY_SCHEMA_INVALID")
    return scrub(dict(mode="DEMO", captured_at=datetime.now(timezone.utc).isoformat(),
                      account_hash=sha256(account.encode()).hexdigest(), start=start, end=end,
                      transactions=transactions, activity=activity, accounting_verified=False))


async def capture(start, end, output):
    from .async_capital import AsyncCapitalDemo
    from .capital import load_credentials
    history_query(start, end)  # 無效時間不得登入。
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        client = AsyncCapitalDemo()
        try:
            await client.login(load_credentials())
            evidence = await collect_history(client, start, end)
            json.dump(evidence, stream, ensure_ascii=False, default=str, indent=2)
            return dict(output=str(path.resolve()), mode="DEMO",
                        transaction_count=len(evidence["transactions"]["transactions"]),
                        activity_count=len(evidence["activity"]["activities"]), orders_submitted=0)
        finally:
            await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(capture(args.start, args.end, args.output))
    except Exception as exc:
        # 不回顯可能包含帳務或認證資料的例外原文。
        parser.exit(1, f"DEMO_HISTORY_PROBE_FAILED ({type(exc).__name__}); details suppressed\n")
    print(json.dumps(result))
