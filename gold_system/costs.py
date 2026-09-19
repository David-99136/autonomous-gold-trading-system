"""持久化 USD 費用上限。預留不等於實際支出，結果不明不可釋放額度。"""
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .core import Signal


class BudgetRejected(ValueError):
    pass


def month_of(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone required")
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def amount(value, *, positive=False):
    # 不接受 float，避免把二進位浮點誤差帶入費用帳。
    if (not isinstance(value, Decimal) or not value.is_finite()
            or value < 0 or (positive and value == 0)):
        raise ValueError("Finite nonnegative Decimal USD amount required")
    return value


class CostLedger:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cost_limits (
                month TEXT PRIMARY KEY, cap_usd TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cost_requests (
                request_id TEXT PRIMARY KEY, month TEXT NOT NULL,
                category TEXT NOT NULL, reserved_usd TEXT NOT NULL,
                actual_usd TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        self.db.commit()

    @contextmanager
    def transaction(self):
        # 跨連線也串列化「查剩餘額度→預留」，避免兩個代理同時花掉同一份預算。
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, kind, **values):
        # 僅傳入本模組固定的帳務欄位，不接受 provider 回應／prompt／例外文字。
        self.db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), kind,
                         json.dumps(values, default=str)))

    def configure_month(self, now, cap_usd):
        """呼叫端必須先取得使用者核准。本方法不自動設定或增加任何月額度。

        同月額度凍結；變更政策須另外設計明確授權流程，不能由分析代理修改。
        """
        month = month_of(now)
        amount(cap_usd, positive=True)
        with self.transaction():
            row = self.db.execute("SELECT cap_usd FROM cost_limits WHERE month=?", (month,)).fetchone()
            if row:
                if Decimal(row[0]) != cap_usd:
                    raise BudgetRejected("MONTHLY_LIMIT_FROZEN")
                return
            self.db.execute("INSERT INTO cost_limits VALUES(?,?)", (month, str(cap_usd)))
            self.event("BUDGET_CONFIGURED", month=month, cap_usd=cap_usd)

    def _status(self, month):
        row = self.db.execute("SELECT cap_usd FROM cost_limits WHERE month=?", (month,)).fetchone()
        cap = Decimal(row[0]) if row else None
        spent, reserved, unknown, overrun = Decimal(0), Decimal(0), 0, False
        for reserve, actual, state in self.db.execute(
                "SELECT reserved_usd,actual_usd,state FROM cost_requests WHERE month=?", (month,)):
            if actual is None:
                reserved += Decimal(reserve)
            else:
                spent += Decimal(actual)
            unknown += state == "UNKNOWN"
            overrun |= state == "OVERRUN"
        remaining = None if cap is None else max(Decimal(0), cap - spent - reserved)
        return {"month": month, "currency": "USD", "cap_usd": cap, "spent_usd": spent,
                "reserved_usd": reserved, "remaining_usd": remaining,
                "unknown_requests": unknown, "overrun": overrun,
                "analysis_available": cap is not None and remaining > 0 and not unknown and not overrun}

    def status(self, now):
        with self.transaction():
            return self._status(month_of(now))

    def reserve(self, request_id, category, maximum_usd, now):
        """回傳 False 表示此意圖已用過，禁止再次呼叫付費服務。"""
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("Invalid cost request ID")
        if category not in ("AI", "DATA", "HOSTING"):
            raise ValueError("Unknown cost category")
        amount(maximum_usd, positive=True)
        month = month_of(now)
        with self.transaction():
            prior = self.db.execute("SELECT category,reserved_usd FROM cost_requests WHERE request_id=?",
                                    (request_id,)).fetchone()
            if prior:
                if prior[0] != category or Decimal(prior[1]) != maximum_usd:
                    raise BudgetRejected("COST_REQUEST_CONFLICT")
                return False
            status = self._status(month)
            if not status["analysis_available"] or maximum_usd > status["remaining_usd"]:
                raise BudgetRejected("BUDGET_UNAVAILABLE")
            self.db.execute("INSERT INTO cost_requests VALUES(?,?,?,?,NULL,'PENDING',?)",
                            (request_id, month, category, str(maximum_usd), now.isoformat()))
            self.event("COST_RESERVED", request_id=request_id, category=category,
                       month=month, reserved_usd=maximum_usd)
            return True

    def mark_unknown(self, request_id):
        with self.transaction():
            row = self.db.execute("SELECT state FROM cost_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise BudgetRejected("COST_REQUEST_MISSING")
            if row[0] == "PENDING":
                self.db.execute("UPDATE cost_requests SET state='UNKNOWN' WHERE request_id=?", (request_id,))
                self.event("COST_UNKNOWN", request_id=request_id)

    def settle(self, request_id, actual_usd):
        """必須使用可追溯用量／帳單。即使超額也如實記帳，不能把數字截到上限。"""
        amount(actual_usd)
        with self.transaction():
            row = self.db.execute("SELECT reserved_usd,actual_usd,state FROM cost_requests WHERE request_id=?",
                                  (request_id,)).fetchone()
            if row is None:
                raise BudgetRejected("COST_REQUEST_MISSING")
            if row[1] is not None:
                if Decimal(row[1]) != actual_usd:
                    raise BudgetRejected("COST_SETTLEMENT_CONFLICT")
                return row[2]
            state = "OVERRUN" if actual_usd > Decimal(row[0]) else "SETTLED"
            self.db.execute("UPDATE cost_requests SET actual_usd=?,state=? WHERE request_id=?",
                            (str(actual_usd), state, request_id))
            self.event("COST_" + state, request_id=request_id, actual_usd=actual_usd)
            return state


@dataclass(frozen=True)
class AnalysisReceipt:
    signal: Signal
    actual_usd: Decimal


async def budgeted_analysis(ledger, request_id, maximum_usd, now, provider):
    """供未來 AI adapter 呼叫的入口；本模組不選模型、不持有憑證、不產生訂單。

    provider 必須自行以凍結價格表及請求限制算出可信的 maximum_usd，並回傳
    可追溯實際費用。尚未實作的 provider 不可用估計值假冒帳單。
    """
    if not ledger.reserve(request_id, "AI", maximum_usd, now):
        raise BudgetRejected("DUPLICATE_ANALYSIS_REQUEST")
    try:
        receipt = await provider()
        if not isinstance(receipt, AnalysisReceipt) or not isinstance(receipt.signal, Signal):
            raise ValueError("Invalid analysis receipt")
        state = ledger.settle(request_id, receipt.actual_usd)
        if state == "OVERRUN":
            raise BudgetRejected("PROVIDER_COST_OVERRUN")
        return receipt.signal
    except BaseException as exc:
        # Cancellation/逾時不代表服務商未計費。保留額度，待帳單對帳。
        ledger.mark_unknown(request_id)
        if not isinstance(exc, Exception) or isinstance(exc, BudgetRejected):
            raise
        # 服務商例外可能夾帶 URL/query/key，不向 CLI 或上層日誌傳回原文。
        raise RuntimeError("ANALYSIS_PROVIDER_FAILED; cost outcome requires reconciliation") from None
