"""已核對成交的帳務彙總。只有權威清單與淨額吻合才可供每日風控使用。"""
import json
import re
from datetime import timezone
from decimal import Decimal, localcontext
from hashlib import sha256

from .reporting import timestamp


def fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("ACCOUNTING_FINGERPRINT_INVALID")
    return value


def money(value, *, positive=False):
    # 限制輸入精度，避免 Decimal 預設 context 在金額加總時默默捨入。
    if (not isinstance(value, Decimal) or not value.is_finite()
            or len(value.as_tuple().digits) > 28 or abs(value.adjusted()) > 28
            or (positive and value <= 0)):
        raise ValueError("ACCOUNTING_AMOUNT_INVALID")
    return value


def window(mode, account_hash, start, end):
    fingerprint(account_hash)
    start, end = timestamp(start), timestamp(end)
    if mode not in ("PAPER", "DEMO") or not 0 < (end - start).total_seconds() <= 86400:
        raise ValueError("ACCOUNTING_WINDOW_INVALID")
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AccountingLedger:
    """可信 Gateway 才能提供 statement 證據；本層不驗證任意 JSON 的來源真偽。"""

    def __init__(self, db, *, initialize=True):
        self.db = db
        if not initialize:  # 報告使用 mode=ro 的連線，不建立 schema。
            return
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS segment_fee_evidence (
                event_id TEXT PRIMARY KEY, extra_fee TEXT NOT NULL, evidence_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounting_receipts (
                mode TEXT NOT NULL, account_hash TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(mode,account_hash,start,end));
        ''')
        self.db.commit()

    def record_fee(self, event_id, extra_fee, evidence_hash):
        """只補尚未計入 realized_pnl 的費用；證實無額外費用才可明確填 0。"""
        fingerprint(event_id)
        fingerprint(evidence_hash)
        money(extra_fee)
        if extra_fee < 0:
            raise ValueError("ACCOUNTING_NEGATIVE_FEE")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT body FROM close_segments WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("ACCOUNTING_SEGMENT_MISSING")
            original = json.loads(row[0]).get("extra_fee")
            if original is not None and Decimal(original) != extra_fee:
                raise ValueError("ACCOUNTING_FEE_CONFLICT")
            old = self.db.execute("SELECT extra_fee,evidence_hash FROM segment_fee_evidence WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if Decimal(old[0]) != extra_fee or old[1] != evidence_hash:
                    raise ValueError("ACCOUNTING_FEE_CONFLICT")
            else:
                self.db.execute("INSERT INTO segment_fee_evidence VALUES(?,?,?)", (event_id, str(extra_fee), evidence_hash))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _snapshot(self, mode, account_hash, start, end):
        # 呼叫者持有 transaction，receipt 與金額必須出自同一資料庫快照。
        rows, fills, unknown = [], [], 0
        with localcontext() as ctx:
            ctx.prec = 80
            gross, fees = Decimal(0), Decimal(0)
            for key, raw, resolved_fee, fee_hash in self.db.execute('''
                    SELECT c.event_id,c.body,f.extra_fee,f.evidence_hash FROM close_segments c
                    LEFT JOIN segment_fee_evidence f ON f.event_id=c.event_id ORDER BY c.event_id'''):
                body = json.loads(raw)
                if body["mode"] != mode or body["account_hash"] != account_hash:
                    continue
                occurred = timestamp(body["occurred_at"]).astimezone(timezone.utc).isoformat()
                if not start <= occurred < end:
                    continue
                pnl = money(Decimal(body["realized_pnl"]))
                original_fee = body["extra_fee"]
                if original_fee is not None and resolved_fee is not None and Decimal(original_fee) != Decimal(resolved_fee):
                    raise ValueError("ACCOUNTING_FEE_CONFLICT")
                fee = resolved_fee if resolved_fee is not None else original_fee
                gross += pnl
                if fee is None:
                    unknown += 1
                else:
                    fee = money(Decimal(fee))
                    if fee < 0:
                        raise ValueError("ACCOUNTING_NEGATIVE_FEE")
                    fees += fee
                fills.append(fingerprint(body["fill_hash"]))
                rows.append([key, body, None if fee is None else str(fee), fee_hash])
            if len(fills) != len(set(fills)):
                raise ValueError("ACCOUNTING_DUPLICATE_FILL")
            return dict(mode=mode, account_hash=account_hash, start=start, end=end,
                        segment_count=len(rows), fill_hashes=sorted(fills),
                        gross_pnl=str(gross), extra_fees=None if unknown else str(fees),
                        net_pnl=None if unknown else str(gross - fees), unknown_fee_segments=unknown,
                        ledger_digest=digest(rows))

    def reconcile_period(self, *, mode, account_hash, start, end, opening_equity,
                         expected_fill_hashes, broker_net_pnl, evidence_hash):
        """上游提供已完整核對的成交清單與 trade-only 淨損益，不含入金、浮盈或 AI 成本。"""
        start, end = window(mode, account_hash, start, end)
        money(opening_equity, positive=True)
        money(broker_net_pnl)
        fingerprint(evidence_hash)
        if not isinstance(expected_fill_hashes, (list, tuple)):
            raise ValueError("ACCOUNTING_MANIFEST_INVALID")
        wanted = sorted(fingerprint(x) for x in expected_fill_hashes)
        if len(wanted) != len(set(wanted)):
            raise ValueError("ACCOUNTING_MANIFEST_DUPLICATE")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            snapshot = self._snapshot(mode, account_hash, start, end)
            if (snapshot["fill_hashes"] != wanted or snapshot["net_pnl"] is None
                    or Decimal(snapshot["net_pnl"]) != broker_net_pnl):
                raise ValueError("ACCOUNTING_STATEMENT_MISMATCH")
            # 同一帳戶／日起點的權益不可因晚到的 statement 改寫，否則熔斷分母會漂移。
            for (raw,) in self.db.execute("SELECT body FROM accounting_receipts WHERE mode=? AND account_hash=? AND start=?",
                                           (mode, account_hash, start)):
                if Decimal(json.loads(raw)["opening_equity"]) != opening_equity:
                    raise ValueError("ACCOUNTING_OPENING_EQUITY_CONFLICT")
            receipt = dict(snapshot, opening_equity=str(opening_equity), evidence_hash=evidence_hash)
            self.db.execute("INSERT OR REPLACE INTO accounting_receipts VALUES(?,?,?,?,?)",
                            (mode, account_hash, start, end, json.dumps(receipt, sort_keys=True)))
            self.db.commit()
            return receipt
        except BaseException:
            self.db.rollback()
            raise

    def _verified_period(self, mode, account_hash, start, end):
        current = self._snapshot(mode, account_hash, start, end)
        saved = self.db.execute("SELECT body FROM accounting_receipts WHERE mode=? AND account_hash=? AND start=? AND end=?",
                                (mode, account_hash, start, end)).fetchone()
        if saved is None:
            raise ValueError("ACCOUNTING_RECEIPT_MISSING")
        receipt = json.loads(saved[0])
        if (receipt["ledger_digest"] != current["ledger_digest"]
                or receipt["net_pnl"] != current["net_pnl"]):
            raise ValueError("ACCOUNTING_RECEIPT_STALE")
        return receipt

    def verified_period(self, *, mode, account_hash, start, end):
        start, end = window(mode, account_hash, start, end)
        self.db.execute("BEGIN")
        try:
            return self._verified_period(mode, account_hash, start, end)
        finally:
            self.db.rollback()  # 只讀一致快照；不建立或更新 receipt。
