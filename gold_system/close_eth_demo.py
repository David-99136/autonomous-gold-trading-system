"""只用於恢復已知 ETHUSD Demo deal 的受限平倉；不開倉、不改其他部位。"""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from .async_capital import AsyncCapitalDemo
from .capital import load_credentials


async def close(deal_id, output):
    audit = Path(output)
    if audit.exists():
        raise ValueError("RECOVERY_OUTPUT_EXISTS")
    client = AsyncCapitalDemo()
    try:
        await client.login(load_credentials())
        positions = (await client.positions()).get("positions")
        orders = (await client.working_orders()).get("workingOrders")
        matches = [row for row in positions if row.get("position", {}).get("dealId") == deal_id
                   and row.get("market", {}).get("epic") == "ETHUSD"]
        if len(matches) != 1 or orders:
            raise ValueError("RECOVERY_SCOPE_MISMATCH")
        reply, _ = await client._request("DELETE", "/positions/" + quote(deal_id, safe=""))
        reference = reply.get("dealReference")
        if not isinstance(reference, str) or not reference:
            raise ValueError("RECOVERY_REFERENCE_MISSING")
        confirmation = await client.confirmation(reference)
        final_positions = (await client.positions()).get("positions")
        final_orders = (await client.working_orders()).get("workingOrders")
        result = {"outcome": "CLOSED" if confirmation.get("dealStatus") == "ACCEPTED" else "UNCONFIRMED",
                  "position_count": len(final_positions), "working_order_count": len(final_orders)}
        audit.write_text(json.dumps(result), encoding="utf-8")
        return result
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(close(args.deal_id, args.output))))
