"""Capital Demo 全平確認的嚴格轉換；不推定部分平倉盈虧或無時區時間。"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


class SettlementUnverified(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise SettlementUnverified(code)


def _number(value):
    _require(not isinstance(value, bool) and isinstance(value, (str, int, float, Decimal)), "CLOSE_NUMBER_INVALID")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SettlementUnverified("CLOSE_NUMBER_INVALID") from None
    _require(result.is_finite(), "CLOSE_NUMBER_INVALID")
    return result


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def corroborate_close_time(confirmation, activities):
    """dateUTC 是官方 UTC 欄位；reference 可重用，必須唯一且多欄位一致。"""
    _require(isinstance(activities, list), "CLOSE_TIMEZONE_UNVERIFIED")
    try:
        date = datetime.fromisoformat(confirmation["date"])
    except (KeyError, TypeError, ValueError):
        raise SettlementUnverified("CLOSE_TIME_INVALID") from None
    _require(date.tzinfo is None, "CLOSE_TIME_ALREADY_AWARE")
    candidates = []
    for row in activities:
        if not isinstance(row, dict) or not isinstance(row.get("details"), dict):
            continue
        detail = row["details"]
        if not (row.get("type") == "POSITION" and row.get("status") == "ACCEPTED"
                and row.get("dealId") == confirmation.get("dealId")
                and row.get("epic") == confirmation.get("epic")
                and detail.get("dealReference") == confirmation.get("dealReference")
                and detail.get("currency") == confirmation.get("profitCurrency")
                and detail.get("direction") == confirmation.get("direction")
                and detail.get("direction") in ("BUY", "SELL")):
            continue
        try:
            utc = datetime.fromisoformat(row["dateUTC"])
        except (KeyError, TypeError, ValueError):
            continue
        if utc.utcoffset() is not None:
            utc = utc.astimezone(timezone.utc)
        else:
            utc = utc.replace(tzinfo=timezone.utc)  # 只適用官方 dateUTC，不是任意 date。
        if (utc.replace(tzinfo=None) == date
                and _number(detail.get("size")) == _number(confirmation.get("size"))
                and _number(detail.get("level")) == _number(confirmation.get("level"))):
            candidates.append(utc)
    _require(len(candidates) == 1, "CLOSE_UTC_CORRELATION_UNVERIFIED")
    return candidates[0]


def normalize_full_close(*, session_before, session_after, confirmation,
                         positions_before, positions_after, expected_reference,
                         expected_deal_id, expected_epic, activities=None):
    """只供可信 Demo Gateway 呼叫；預期 ID 來自原始訂單意圖，不從確認反填。"""
    _require(all(isinstance(x, dict) for x in (session_before, session_after, confirmation)), "CLOSE_SHAPE_INVALID")
    _require(all(isinstance(x, str) and x for x in (expected_reference, expected_deal_id)), "CLOSE_EXPECTATION_MISSING")
    _require(expected_epic in ("GOLD", "ETHUSD"), "CLOSE_EPIC_INVALID")
    account = session_before.get("accountId")
    _require(isinstance(account, str) and bool(account)
             and session_after.get("accountId") == account
             and session_before.get("currency") == session_after.get("currency") == "USD", "CLOSE_ACCOUNT_MISMATCH")
    c = confirmation
    _require(c.get("dealReference") == expected_reference and c.get("dealId") == expected_deal_id
             and c.get("epic") == expected_epic, "CLOSE_IDENTITY_MISMATCH")
    _require(c.get("dealStatus") == "ACCEPTED" and c.get("status") == "CLOSED", "CLOSE_NOT_CONFIRMED")
    affected = c.get("affectedDeals")
    _require(isinstance(affected, list) and len(affected) == 1 and isinstance(affected[0], dict)
             and affected[0].get("dealId") == expected_deal_id
             and affected[0].get("status") == "FULLY_CLOSED", "CLOSE_AFFECTED_UNVERIFIED")
    _require(c.get("profitCurrency") == "USD", "CLOSE_CURRENCY_INVALID")
    size, profit = _number(c.get("size")), _number(c.get("profit"))
    _require(size > 0, "CLOSE_SIZE_INVALID")
    try:
        occurred = datetime.fromisoformat(c["date"])
    except (KeyError, TypeError, ValueError):
        raise SettlementUnverified("CLOSE_TIME_INVALID") from None
    if occurred.utcoffset() is None:
        occurred = corroborate_close_time(c, activities)
    _require(isinstance(positions_before, list) and isinstance(positions_after, list), "CLOSE_SNAPSHOT_INVALID")
    for row in positions_before + positions_after:
        _require(isinstance(row, dict) and isinstance(row.get("position"), dict)
                 and isinstance(row["position"].get("dealId"), str)
                 and bool(row["position"]["dealId"]), "CLOSE_SNAPSHOT_INVALID")
    before = [r for r in positions_before if r["position"]["dealId"] == expected_deal_id]
    after = [r for r in positions_after if r["position"]["dealId"] == expected_deal_id]
    _require(len(before) == 1 and not after, "CLOSE_EXPOSURE_UNVERIFIED")
    row = before[0]
    _require(isinstance(row.get("market"), dict) and row["market"].get("epic") == expected_epic
             and row["position"].get("currency") == "USD", "CLOSE_POSITION_MISMATCH")
    _require(_number(row["position"].get("size")) == size, "CLOSE_QUANTITY_MISMATCH")
    # 只雜湊白名單欄位；原始回應或 session token 不進帳本。
    evidence = {k: c[k] for k in ("dealReference", "dealId", "epic", "dealStatus", "status",
                                "date", "size", "profit", "profitCurrency", "affectedDeals")}
    evidence["corroborated_utc"] = occurred.astimezone(timezone.utc).isoformat()
    # 與 reconciliation.evaluate／history probe 使用相同帳戶指紋規則。
    return dict(mode="DEMO", account_hash=sha256(account.encode()).hexdigest(),
                fill_hash=_fingerprint([expected_reference, expected_deal_id]),
                trade_hash=_fingerprint(expected_deal_id), evidence_hash=_fingerprint(evidence),
                occurred_at=occurred, quantity=size, realized_pnl=profit, extra_fee=None)


def ingest_full_close(ledger, **evidence):
    # 驗證失敗前不呼叫入帳，成功後沿用帳務／通知同一 transaction。
    return ledger.record_close(**normalize_full_close(**evidence))
