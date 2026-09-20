"""已核對平倉分段與通知原子入庫；本模組不讀券商、不送通知。"""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256


def canonical_close(*, mode, account_hash, fill_hash, trade_hash, evidence_hash,
                    occurred_at, quantity, realized_pnl, extra_fee=None):
    """上游須先確認成交；extra_fee 只填尚未包含在 P&L 的額外費用。"""
    if mode not in ("PAPER", "DEMO"):
        raise ValueError("SETTLEMENT_MODE_INVALID")
    for value in (account_hash, fill_hash, trade_hash, evidence_hash):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("SETTLEMENT_FINGERPRINT_REQUIRED")
    if not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
        raise ValueError("SETTLEMENT_TIME_INVALID")
    for value in (quantity, realized_pnl, extra_fee):
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
            raise ValueError("SETTLEMENT_AMOUNT_INVALID")
    if quantity is None or quantity <= 0 or realized_pnl is None or (extra_fee is not None and extra_fee < 0):
        raise ValueError("SETTLEMENT_AMOUNT_INVALID")
    return dict(mode=mode, account_hash=account_hash, fill_hash=fill_hash,
                trade_hash=trade_hash, evidence_hash=evidence_hash, currency="USD",
                occurred_at=occurred_at.astimezone(timezone.utc).isoformat(),
                quantity=str(quantity.normalize()), realized_pnl=str(realized_pnl.normalize()),
                extra_fee=None if extra_fee is None else str(extra_fee.normalize()))


class SettlementLedger:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS close_segments (
                event_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settlement_notifications (
                event_id TEXT PRIMARY KEY REFERENCES close_segments(event_id),
                state TEXT NOT NULL CHECK(state IN ('PENDING','IN_FLIGHT','SENT','UNKNOWN')));
        ''')
        self.db.commit()

    def record_close(self, **values):
        body = canonical_close(**values)
        key = sha256(json.dumps([body[k] for k in ("mode", "account_hash", "fill_hash")]).encode()).hexdigest()
        encoded = json.dumps(body, sort_keys=True)
        # 跨連線序列化重複檢查，且帳務成功、通知入列失敗時必須一併回滾。
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute("SELECT body FROM close_segments WHERE event_id=?", (key,)).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise ValueError("SETTLEMENT_CONFLICT")
            else:
                self.db.execute("INSERT INTO close_segments VALUES(?,?)", (key, encoded))
                self.db.execute("INSERT INTO settlement_notifications VALUES(?,'PENDING')", (key,))
            self.db.commit()
            return key
        except BaseException:
            self.db.rollback()
            raise

    def claim_notification(self, event_id):
        """只有一個 worker 能取得 PENDING；重啟不重設 IN_FLIGHT。"""
        with self.db:
            changed = self.db.execute(
                "UPDATE settlement_notifications SET state='IN_FLIGHT' WHERE event_id=? AND state='PENDING'",
                (event_id,)).rowcount
        if not changed:
            return None
        row = self.db.execute("SELECT body FROM close_segments WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row[0])

    def finish_notification(self, event_id, outcome):
        # RETRY 僅代表通道已確認完全沒有投遞；timeout 應使用 UNKNOWN。
        target = {"DELIVERED": "SENT", "NOT_DELIVERED": "PENDING", "UNKNOWN": "UNKNOWN"}.get(outcome)
        if target is None:
            raise ValueError("NOTIFICATION_OUTCOME_INVALID")
        with self.db:
            changed = self.db.execute(
                "UPDATE settlement_notifications SET state=? WHERE event_id=? AND state='IN_FLIGHT'",
                (target, event_id)).rowcount
            if not changed:
                raise ValueError("NOTIFICATION_TRANSITION_INVALID")

    def pending_ids(self):
        return [row[0] for row in self.db.execute(
            "SELECT event_id FROM settlement_notifications WHERE state='PENDING' ORDER BY event_id")]
