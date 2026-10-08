"""只用於恢復已知 ETHUSD Demo deal 的受限平倉；不開倉、不改其他部位。"""
import argparse
import asyncio
import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from .async_capital import AsyncCapitalDemo
from .capital import load_credentials
from .store import Store


# [Codex | 2026-09-27] 輸出只供摘要；Store 的意圖／事件才是恢復時的權威紀錄。
def _write_result(audit, result):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=audit.parent,
                                         prefix=".eth-recovery-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, audit)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _positions(payload):
    rows = payload.get("positions")
    if not isinstance(rows, list):
        raise ValueError("RECOVERY_SNAPSHOT_INVALID")
    result = {}
    for row in rows:
        position, market = row.get("position", {}), row.get("market", {})
        deal, epic = position.get("dealId"), market.get("epic")
        if not isinstance(deal, str) or not deal or not isinstance(epic, str) or not epic or deal in result:
            raise ValueError("RECOVERY_SNAPSHOT_INVALID")
        result[deal] = epic
    return result


def _orders(payload):
    rows = payload.get("workingOrders")
    if not isinstance(rows, list):
        raise ValueError("RECOVERY_SNAPSHOT_INVALID")
    return rows


def _account(session):
    account = session.get("accountId")
    if not isinstance(account, str) or not account or session.get("currency") != "USD":
        raise ValueError("RECOVERY_ACCOUNT_INVALID")
    return sha256(account.encode()).hexdigest()


# [Codex | 2026-09-27] 有界減風險入口：既有資料庫、持久化新倉鎖、同一 deal 不重送。
async def close(deal_id, output, *, database=None, transport=None):
    if not isinstance(deal_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", deal_id):
        raise ValueError("RECOVERY_DEAL_INVALID")
    audit = Path(output)
    if audit.exists():
        raise ValueError("RECOVERY_OUTPUT_EXISTS")
    database = database or Path(__file__).resolve().parent.parent / "runtime" / "demo-audit.db"
    store = Store(database, must_exist=True)
    client = AsyncCapitalDemo(transport=transport)
    claimed, intent = False, None
    result = {"outcome": "RECOVERY_LOCKED", "entries_enabled": False,
              "position_count": None, "working_order_count": None}
    try:
        # 排他建立，不能以 exists() 檢查後覆寫另一個執行者的輸出。
        with audit.open("x", encoding="utf-8") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        await client.login(load_credentials())
        account_hash = _account(await client.session())
        positions = _positions(await client.positions())
        orders = _orders(await client.working_orders())
        if positions.get(deal_id) != "ETHUSD" or orders:
            raise ValueError("RECOVERY_SCOPE_MISMATCH")
        intent = "eth-close-" + sha256((account_hash + ":" + deal_id).encode()).hexdigest()
        store.stop_new_entries()  # 成功平倉也不自動解除新倉鎖。
        if not store.claim(intent):
            raise ValueError("RECOVERY_ALREADY_RECORDED")
        claimed = True
        store.emit("ETH_RECOVERY_INTENT", intent_id=intent, account_hash=account_hash,
                   deal_id=deal_id, epic="ETHUSD", state="RECOVERY_LOCKED")
        if _account(await client.session()) != account_hash:
            raise ValueError("RECOVERY_ACCOUNT_CHANGED")
        reply, _ = await client._request("DELETE", "/positions/" + quote(deal_id, safe=""))
        reference = reply.get("dealReference")
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reference):
            raise ValueError("RECOVERY_REFERENCE_MISSING")
        store.emit("ETH_RECOVERY_REFERENCE", intent_id=intent, reference=reference)
        confirmation = await client.confirmation(reference)
        final_positions = _positions(await client.positions())
        final_orders = _orders(await client.working_orders())
        same_account = _account(await client.session()) == account_hash
        # 接受請求不等於已執行；指定 deal 須消失，其他部位不可無故增減。
        closed = (same_account and confirmation.get("dealStatus") == "ACCEPTED"
                  and confirmation.get("dealReference") == reference
                  and final_positions == {key: epic for key, epic in positions.items() if key != deal_id}
                  and not final_orders)
        result.update(outcome="CLOSED" if closed else "RECOVERY_LOCKED",
                      position_count=len(final_positions), working_order_count=len(final_orders))
        store.emit("ETH_RECOVERY_RESULT", intent_id=intent, **result)
        _write_result(audit, result)
        store.finish(intent, "CONFIRMED" if closed else "UNKNOWN")
        return result
    except BaseException as exc:
        if not claimed:
            raise
        # 逾時／取消／落盤失敗都不重送。即使再寫入失敗，送出前的 PENDING 仍保留。
        result.update(outcome="RECOVERY_LOCKED", reason="RECOVERY_UNRESOLVED")
        store.finish(intent, "UNKNOWN")
        store.emit("ETH_RECOVERY_RESULT", intent_id=intent, **result)
        try:
            _write_result(audit, result)
        except OSError:
            pass
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return result
    finally:
        try:
            await client.close()
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database", help="Existing Demo audit database; never creates an empty replacement")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(close(args.deal_id, args.output, database=args.database))))
