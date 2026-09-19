"""唯讀券商對帳。符合快照不等於解鎖；不確定訂單絕不推定為失敗。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import time


@dataclass(frozen=True)
class ExpectedPosition:
    deal_id: str
    direction: str
    size: Decimal
    stop: Decimal
    entry: Decimal
    epic: str = "GOLD"

    def __post_init__(self):
        if (not self.deal_id or self.epic != "GOLD" or self.direction not in ("BUY", "SELL")
                or any(not x.is_finite() or x <= 0 for x in (self.size, self.stop, self.entry))):
            raise ValueError("Invalid expected position")


@dataclass(frozen=True)
class Snapshot:
    # 原始資料只存在記憶體；repr 與一般報告不洩漏帳號及原始 API 欄位。
    session_before: dict = field(repr=False)
    session_after: dict = field(repr=False)
    preferences: dict = field(repr=False)
    positions: dict = field(repr=False)
    orders: dict = field(repr=False)
    activity: dict = field(repr=False)
    received_at: datetime
    elapsed_seconds: float


def collect(client):
    """序列讀取前後帳戶；任何例外交由呼叫者鎖定，不以空陣列取代失敗。"""
    start = time.monotonic()
    before = client.session()
    preferences = client.preferences()
    positions = client.positions()
    orders = client.working_orders()
    activity = client.activity()
    after = client.session()
    return Snapshot(before, after, preferences, positions, orders, activity,
                    datetime.now(timezone.utc), time.monotonic() - start)


async def collect_async(client):
    """沿用相同前後帳戶檢查；非同步 I/O 不佔住交易服務的事件迴圈。"""
    start = time.monotonic()
    before = await client.session()
    preferences = await client.preferences()
    positions = await client.positions()
    orders = await client.working_orders()
    activity = await client.activity()
    after = await client.session()
    return Snapshot(before, after, preferences, positions, orders, activity,
                    datetime.now(timezone.utc), time.monotonic()-start)


async def inspect_demo_async(client, store):
    from .order_journal import OrderJournal
    try:
        unresolved = store.unresolved_intents() + OrderJournal(store).unresolved()
        report = evaluate(await collect_async(client), unresolved_intents=unresolved)
    except Exception:
        report = {"state": "RECOVERY_LOCKED", "reasons": ["BROKER_READ_FAILED"],
                  "entries_enabled": False}
    store.set("broker_reconciliation", report)
    store.emit("BROKER_RECONCILIATION", **report)
    return report


def number(value):
    if isinstance(value, bool):
        raise ValueError("Invalid broker number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid broker number") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("Invalid broker number")
    return parsed


def evaluate(snapshot, expected=(), *, now=None, expected_account_hash=None, unresolved_intents=()):
    """比對完整持倉集合；未知掛單保守鎖定，不自動取消使用者訂單。

    即使資料吻合，也只表示這次讀取一致。REST 並非原子快照，後續下單前
    仍需新鮮行情、帳戶風控與明確啟用；此函數永遠不授予下單能力。
    """
    now = now or datetime.now(timezone.utc)
    reasons = []
    position_count = order_count = None
    fingerprint = None
    try:
        age = (now - snapshot.received_at).total_seconds()
        if not 0 <= age <= 2 or not 0 <= snapshot.elapsed_seconds <= 2:
            reasons.append("SNAPSHOT_STALE_OR_SLOW")
        account = snapshot.session_before["accountId"]
        if not isinstance(account, str) or not account:
            raise ValueError("Missing account")
        fingerprint = sha256(account.encode()).hexdigest()
        if account != snapshot.session_after["accountId"]:
            reasons.append("ACCOUNT_CHANGED")
        if expected_account_hash is not None and fingerprint != expected_account_hash:
            reasons.append("ACCOUNT_MISMATCH")
        if any(s["currency"] != "USD" for s in (snapshot.session_before, snapshot.session_after)):
            reasons.append("ACCOUNT_CURRENCY")
        if snapshot.preferences["hedgingMode"] is not False:
            reasons.append("NETTING_MODE_UNPROVEN")
        positions, orders = snapshot.positions["positions"], snapshot.orders["workingOrders"]
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise ValueError("Invalid lists")
        if not isinstance(snapshot.activity["activities"], list):
            raise ValueError("Invalid activity")
        position_count, order_count = len(positions), len(orders)
        if orders:
            reasons.append("WORKING_ORDERS_REQUIRE_RECONCILIATION")
        if len(positions) > 1:
            reasons.append("MULTIPLE_POSITIONS")
        wanted = {p.deal_id: p for p in expected}
        if len(wanted) != len(expected):
            raise ValueError("Duplicate expectations")
        seen = set()
        for item in positions:
            p, market = item["position"], item["market"]
            deal = p["dealId"]
            if not isinstance(deal, str) or not deal or deal in seen:
                raise ValueError("Duplicate or missing position ID")
            seen.add(deal)
            if market["epic"] != "GOLD" or p["currency"] != "USD":
                reasons.append("UNEXPECTED_INSTRUMENT")
            size, entry = number(p["size"]), number(p["level"])
            if p["direction"] not in ("BUY", "SELL"):
                raise ValueError("Invalid direction")
            # 缺少 stopLevel 不能推論已有券商停損。
            stop = number(p.get("stopLevel"))
            local = wanted.get(deal)
            if local is None:
                reasons.append("UNTRACKED_POSITION")
            elif (size != local.size or entry != local.entry or p["direction"] != local.direction
                  or market["epic"] != local.epic):
                reasons.append("POSITION_MISMATCH")
            elif (stop - local.stop) * (1 if local.direction == "BUY" else -1) < 0:
                reasons.append("STOP_LOOSENED")
        if set(wanted) - seen:
            reasons.append("EXPECTED_POSITION_MISSING")
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append("BROKER_SCHEMA_OR_STOP_UNPROVEN")
    if unresolved_intents:
        # 空倉與最近活動紀錄均不足以證明逾時訂單從未成交。
        reasons.append("UNRESOLVED_INTENT")
    return {"state": "RECOVERY_LOCKED" if reasons else "SNAPSHOT_MATCHED",
            "reasons": sorted(set(reasons)), "position_count": position_count,
            "working_order_count": order_count, "account_hash": fingerprint,
            "received_at": snapshot.received_at.isoformat(),
            "elapsed_seconds": snapshot.elapsed_seconds, "entries_enabled": False}


def inspect_demo(client, store, expected=(), expected_account_hash=None):
    """將安全摘要落盤；查詢失敗時不保存可能包含私密資料的例外文字。"""
    try:
        report = evaluate(collect(client), expected, expected_account_hash=expected_account_hash,
                          unresolved_intents=store.unresolved_intents())
    except Exception:
        report = {"state": "RECOVERY_LOCKED", "reasons": ["BROKER_READ_FAILED"],
                  "position_count": None, "working_order_count": None, "entries_enabled": False}
    store.set("broker_reconciliation", report)
    store.emit("BROKER_RECONCILIATION", **report)
    return report


def verify_open_confirmation(reference, payload, expected, snapshot, *, now=None,
                             expected_account_hash=None):
    """成交確認與持倉必須同時吻合，回傳值不會清除 journal 或解除交易鎖。

    expected.entry 應為待驗證的實際成交價，不是送單前估價。多個 affectedDeals
    或部分成交尚未有驗證過的聚合規則，明確回報 UNKNOWN 交由恢復流程處理。
    """
    reasons = []
    status = "UNKNOWN"
    try:
        if not reference or payload["dealReference"] != reference:
            reasons.append("CONFIRMATION_REFERENCE_MISMATCH")
        elif payload["dealStatus"] == "REJECTED":
            # 拒絕回應仍非空倉證明，外層仍必須執行帳戶對帳。
            if payload.get("affectedDeals", []) == []:
                return {"outcome": "REJECTED", "reasons": [], "entries_enabled": False}
            reasons.append("REJECTED_WITH_AFFECTED_DEALS")
        elif payload["dealStatus"] != "ACCEPTED" or payload["status"] != "OPEN":
            reasons.append("CONFIRMATION_NOT_OPEN")
        else:
            affected = payload["affectedDeals"]
            if (not isinstance(affected, list) or len(affected) != 1
                    or affected[0]["dealId"] != expected.deal_id
                    or affected[0]["status"] != "OPENED"):
                reasons.append("AFFECTED_DEALS_UNPROVEN")
            if (payload["epic"] != expected.epic or payload["direction"] != expected.direction
                    or number(payload["size"]) != expected.size
                    or number(payload["level"]) != expected.entry):
                reasons.append("CONFIRMED_FILL_MISMATCH")
            result = evaluate(snapshot, (expected,), now=now, expected_account_hash=expected_account_hash)
            reasons.extend(result["reasons"])
            if not reasons:
                status = "CONFIRMED"
    except (KeyError, ValueError, TypeError, AttributeError, IndexError):
        reasons.append("CONFIRMATION_SCHEMA_UNPROVEN")
    return {"outcome": status, "reasons": sorted(set(reasons)), "entries_enabled": False}


async def reconcile_open_async(client, journal, intent, contract, policy):
    """將 journal 原意圖與實際成交對照；不接受 sender 自行宣告已成交。

    contract 必須由獨立 gateway 從已驗證契約快照提供，不能由分析代理猜每點損益。
    此函數只回傳證據摘要，journal 終態由 dispatch/recovery 協調器提交。
    """
    try:
        row = journal.get(intent)
        request, reference = row["request"], row["reference"]
        if contract.epic != "GOLD" or request["epic"] != contract.epic or request["version"] != policy.version:
            return {"outcome": "UNKNOWN", "reasons": ["CONTRACT_OR_VERSION_MISMATCH"]}
        # 確認暫時查不到時，仍查持倉／掛單／活動；不能跳過恢復所需證據。
        confirmation = None
        if reference:
            try:
                confirmation = await client.confirmation(reference)
            except Exception:
                pass
        snapshot = await collect_async(client)
        if confirmation is None:
            return {"outcome": "UNKNOWN", "reasons": ["CONFIRMATION_UNAVAILABLE"],
                    "snapshot": evaluate(snapshot, expected_account_hash=row["account_hash"])}
        if confirmation.get("dealReference") != reference:
            return {"outcome": "UNKNOWN", "reasons": ["CONFIRMATION_REFERENCE_MISMATCH"]}
        if confirmation.get("dealStatus") == "REJECTED":
            account = evaluate(snapshot, expected_account_hash=row["account_hash"])
            if confirmation.get("affectedDeals", []) == [] and account["state"] == "SNAPSHOT_MATCHED":
                return {"outcome": "REJECTED", "reasons": [], "entries_enabled": False}
            return {"outcome": "UNKNOWN", "reasons": ["REJECTION_ACCOUNT_NOT_RECONCILED"]}
        affected = confirmation["affectedDeals"]
        if not isinstance(affected, list) or len(affected) != 1:
            return {"outcome": "UNKNOWN", "reasons": ["AFFECTED_DEALS_UNPROVEN"]}
        size, fill, stop = number(confirmation["size"]), number(confirmation["level"]), number(request["stopLevel"])
        if size != number(request["size"]) or confirmation["direction"] != request["direction"]:
            return {"outcome": "UNKNOWN", "reasons": ["FILL_SIZE_OR_DIRECTION_MISMATCH"]}
        sign = Decimal(1) if request["direction"] == "BUY" else Decimal(-1)
        if (fill-stop)*sign < contract.minimum_stop:
            return {"outcome": "UNKNOWN", "reasons": ["ACTUAL_STOP_DISTANCE_INVALID"], "entries_enabled": False}
        slippage = (fill-number(request["planned_entry"]))*sign
        stop_risk = max(Decimal(0), (fill-stop)*sign + policy.slippage)*size*contract.value_per_point
        margin = fill*size*contract.value_per_point*contract.margin_rate
        if slippage > policy.slippage or stop_risk > number(request["risk"]) or margin > number(request["margin"]):
            return {"outcome": "UNKNOWN", "reasons": ["ACTUAL_FILL_EXCEEDS_APPROVED_ENVELOPE"],
                    "actual_slippage": str(slippage), "stop_risk": str(stop_risk), "margin": str(margin),
                    "entries_enabled": False}
        expected = ExpectedPosition(affected[0]["dealId"], request["direction"], size, stop, fill)
        return verify_open_confirmation(reference, confirmation, expected, snapshot,
                                        expected_account_hash=row["account_hash"])
    except Exception:
        return {"outcome": "UNKNOWN", "reasons": ["OPEN_RECONCILIATION_FAILED"], "entries_enabled": False}
