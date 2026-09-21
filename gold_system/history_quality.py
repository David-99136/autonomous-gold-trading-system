"""GOLD 原始歷史品質隔離；觀測成功不等於已收棒，更不授予交易權限。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from hashlib import sha256
import json


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("HISTORY_REQUEST_INVALID")
    return value.astimezone(timezone.utc)


def _row(row):
    """只保留 UTC 標記與兩側 OHLC；不把標記改名成 close time。"""
    stamp = datetime.fromisoformat(row["snapshotTimeUTC"])
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    if stamp.second or stamp.microsecond:
        raise ValueError("HISTORY_ROW_INVALID")
    sides = []
    for side in ("bid", "ask"):
        prices = []
        for field in ("open", "high", "low", "close"):
            value = row[field + "Price"][side]
            if type(value) not in (str, int, float, Decimal):
                raise ValueError("HISTORY_ROW_INVALID")
            if len(str(value)) > 80:
                raise ValueError("HISTORY_ROW_INVALID")
            value = Decimal(str(value))
            if not value.is_finite() or value <= 0 or len(value.as_tuple().digits) > 18 or abs(value.adjusted()) > 12:
                raise ValueError("HISTORY_ROW_INVALID")
            prices.append(value)
        sides.append(prices)
    reasons = set()
    for opening, high, low, closing in sides:
        if not low <= min(opening, closing) <= max(opening, closing) <= high:
            reasons.add("HISTORY_OHLC_INVALID")
    if any(bid > ask for bid, ask in zip(*sides)):
        reasons.add("HISTORY_BID_ASK_CROSSED")
    # Decimal 的等價字串（1、1.0）必須得到同一價格指紋。
    def exact_text(price):
        text = format(price, "f")  # 不用 normalize()，避免全域 Decimal precision 改變指紋。
        return text.rstrip("0").rstrip(".") if "." in text else text
    fingerprint = _hash([[exact_text(p) for p in side] for side in sides])
    return stamp, fingerprint, reasons


class HistoryQualityMonitor:
    """只觀測 GOLD Demo MINUTE；必須使用與執行 Guard 相同的 Store 才能阻擋它。"""

    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS history_price_first (
                stamp TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history_quality_observations (
                id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, payload TEXT NOT NULL);
        ''')

    def observe(self, response, *, start, end, received_at, epic="GOLD", resolution="MINUTE"):
        reasons, parsed = set(), []
        received = None
        try:
            start, end, received = _aware(start), _aware(end), _aware(received_at)
            if (epic != "GOLD" or resolution != "MINUTE" or not start <= end <= received
                    or end - start > timedelta(minutes=999)
                    or any(t.second or t.microsecond for t in (start, end))):
                raise ValueError("HISTORY_REQUEST_INVALID")
        except (TypeError, AttributeError, ValueError, OverflowError):
            reasons.add("HISTORY_REQUEST_INVALID")
        if not reasons:
            rows = response.get("prices") if isinstance(response, dict) else None
            if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
                reasons.add("HISTORY_ROWS_INVALID")
            else:
                previous = None
                for row in rows:
                    try:
                        stamp, fingerprint, faults = _row(row)
                        reasons.update(faults)
                        if not start <= stamp <= end or stamp > received:
                            reasons.add("HISTORY_RANGE_INVALID")
                            continue
                        if previous is not None:
                            if stamp <= previous:
                                reasons.add("HISTORY_ORDER_INVALID")
                            elif stamp - previous != timedelta(minutes=1):
                                reasons.add("HISTORY_GAP")
                        previous = stamp
                        parsed.append((stamp.isoformat(), fingerprint))
                    except (TypeError, KeyError, AttributeError, ValueError, OverflowError, DecimalException):
                        reasons.add("HISTORY_ROW_INVALID")
                # 不把缺頭／缺尾的固定窗口誤當作完整資料，亦不自動猜測休市。
                if not parsed or parsed[0][0] != start.isoformat() or parsed[-1][0] != end.isoformat():
                    reasons.add("HISTORY_COVERAGE_INCOMPLETE")
        received_text = received.isoformat() if received is not None else None
        observed = datetime.now(timezone.utc).isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            revisions = 0
            for stamp, fingerprint in parsed:
                old = self.db.execute("SELECT fingerprint,received_at FROM history_price_first WHERE stamp=?", (stamp,)).fetchone()
                if old:
                    if fingerprint != old[0]:
                        revisions += 1
                        reasons.add("HISTORY_PRICE_REVISION")
                    if received_text < old[1]:
                        reasons.add("HISTORY_RECEIPT_BACKDATED")
                else:
                    # 這是首次觀測，不是通過／可交易的價格。異常快照也不能被後值抹除。
                    self.db.execute("INSERT INTO history_price_first VALUES(?,?,?)", (stamp, fingerprint, received_text))
            if reasons:
                self.db.execute("INSERT OR REPLACE INTO state VALUES('market_data_blocked','true')")
            locked = self.store.get("market_data_blocked", False) is not False
            if locked and not reasons:
                reasons.add("HISTORY_RECOVERY_LOCKED")
            result = dict(status="RECOVERY_REQUIRED" if locked else "OBSERVED_UNVERIFIED",
                          reasons=sorted(reasons), observed_rows=len(parsed), revised_rows=revisions,
                          price_fingerprint_hash=_hash(parsed), received_at=received_text,
                          entries_enabled=False, closure_verified=False)
            payload = json.dumps(result, sort_keys=True)
            self.db.execute("INSERT INTO history_quality_observations(received_at,payload) VALUES(?,?)", (observed, payload))
            self.db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)", (observed, "HISTORY_QUALITY_OBSERVED", payload))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise  # 持久化失敗必須由呼叫端停止，不能當成正常資料繼續。
