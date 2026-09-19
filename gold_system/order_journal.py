"""訂單送出與確認的持久化狀態機。不是解鎖介面，也不持有券商憑證。"""
import asyncio
import json
import re
from datetime import datetime, timezone
from hashlib import sha256

from .core import D, Direction, Plan, Rejected


class OrderJournal:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS gateway_orders (
                intent_id TEXT PRIMARY KEY, account_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, request TEXT NOT NULL,
                state TEXT NOT NULL, reference TEXT, updated_at TEXT NOT NULL);
        ''')
        self.db.commit()

    def _event(self, intent_id, state):
        self.db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), "ORDER_STATE",
                         json.dumps({"intent_id": intent_id, "state": state})))

    def prepare(self, plan, epic, account_hash):
        """呼叫端只可傳入獨立風控核准的 Plan；不接受 LLM 自訂 broker payload。

        固定欄位有助稽核，但這不是完整的風控／帳戶權限檢查。
        """
        if (not isinstance(plan, Plan) or epic != "GOLD"
                or not re.fullmatch(r"[a-f0-9]{64}", account_hash)):
            raise Rejected("INVALID_GATEWAY_SCOPE")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", plan.signal.signal_id)
                or plan.signal.direction not in (Direction.LONG, Direction.SHORT)
                or any(not isinstance(x, D) or not x.is_finite() or x <= 0 for x in
                       (plan.entry, plan.size, plan.risk, plan.margin, plan.signal.stop, plan.signal.target))):
            raise Rejected("INVALID_GATEWAY_PLAN")
        request = {"epic": epic, "direction": "BUY" if plan.signal.direction == Direction.LONG else "SELL",
                   "size": str(plan.size), "stopLevel": str(plan.signal.stop),
                   "profitLevel": str(plan.signal.target), "planned_entry": str(plan.entry),
                   "risk": str(plan.risk), "margin": str(plan.margin), "version": plan.signal.version,
                   "mode": plan.signal.mode, "created": plan.signal.created.isoformat(),
                   "expires": plan.signal.expires.isoformat()}
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = sha256(encoded.encode()).hexdigest()
        intent = plan.signal.signal_id
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT request_hash,account_hash FROM gateway_orders WHERE intent_id=?",
                                    (intent,)).fetchone()
            if prior:
                if prior != (digest, account_hash):
                    raise Rejected("INTENT_CONTENT_CONFLICT")
                self.db.commit()
                return False
            self.db.execute("INSERT INTO gateway_orders VALUES(?,?,?,?,'PREPARED',NULL,?)",
                            (intent, account_hash, digest, encoded, datetime.now(timezone.utc).isoformat()))
            self._event(intent, "PREPARED")
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def get(self, intent):
        row = self.db.execute("SELECT state,reference,request,account_hash FROM gateway_orders WHERE intent_id=?",
                              (intent,)).fetchone()
        if row is None:
            raise Rejected("ORDER_INTENT_MISSING")
        return {"state": row[0], "reference": row[1], "request": json.loads(row[2]), "account_hash": row[3]}

    def transition(self, intent, state, *, reference=None):
        allowed = {"SUBMITTING": {"PREPARED"}, "ACKNOWLEDGED": {"SUBMITTING", "UNKNOWN"},
                   "UNKNOWN": {"SUBMITTING", "ACKNOWLEDGED"},
                   "CONFIRMED": {"ACKNOWLEDGED", "UNKNOWN"},
                   "REJECTED": {"ACKNOWLEDGED", "UNKNOWN"}}
        if state not in allowed:
            raise Rejected("INVALID_ORDER_TRANSITION")
        if reference is not None and (not isinstance(reference, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reference)):
            raise Rejected("INVALID_BROKER_REFERENCE")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(intent)
            if current["state"] not in allowed[state]:
                raise Rejected("ORDER_ALREADY_DISPATCHED_OR_TERMINAL")
            if current["reference"] is not None and reference not in (None, current["reference"]):
                raise Rejected("BROKER_REFERENCE_CONFLICT")
            reference = reference or current["reference"]
            if state in ("ACKNOWLEDGED", "CONFIRMED", "REJECTED") and reference is None:
                raise Rejected("BROKER_REFERENCE_REQUIRED")
            if state == "SUBMITTING":
                expires = datetime.fromisoformat(current["request"]["expires"])
                if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                    raise Rejected("PREPARED_ORDER_EXPIRED")
                # 同一帳戶不能同時送出另一個初始開倉；CONFIRMED 亦不等於已平倉。
                active = self.db.execute("SELECT 1 FROM gateway_orders WHERE account_hash=? AND intent_id!=? "
                                         "AND state IN ('SUBMITTING','ACKNOWLEDGED','UNKNOWN','CONFIRMED') LIMIT 1",
                                         (current["account_hash"], intent)).fetchone()
                if active:
                    raise Rejected("ACCOUNT_ORDER_UNRESOLVED_OR_OPEN")
            self.db.execute("UPDATE gateway_orders SET state=?,reference=?,updated_at=? WHERE intent_id=?",
                            (state, reference, datetime.now(timezone.utc).isoformat(), intent))
            self._event(intent, state)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def unresolved(self):
        return [r[0] for r in self.db.execute("SELECT intent_id FROM gateway_orders "
                "WHERE state IN ('SUBMITTING','ACKNOWLEDGED','UNKNOWN') ORDER BY intent_id")]


async def dispatch_once(journal, intent, send, reconcile, *, timeout_seconds=2):
    """流程核心，已與 AsyncCapitalDemo 做 mock HTTP 整合，正式風控／對帳器尚未接線。

    send() 只能送出已核准意圖一次。reconcile(reference) 必須檢查帳戶、
    確認、持倉、停損與活動，回傳 CONFIRMED/REJECTED/UNKNOWN。
    取消 send 的等待不能保證網路請求未到券商，因此逾時永遠保留 UNKNOWN。
    """
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 2:
        raise ValueError("Confirmation deadline must be between zero and two seconds")
    journal.transition(intent, "SUBMITTING")  # 必須在任何外部副作用之前 durable commit。
    reference = None
    try:
        reply = await asyncio.wait_for(send(), timeout=timeout_seconds)
        reference = reply["dealReference"]
        journal.transition(intent, "ACKNOWLEDGED", reference=reference)
    except asyncio.CancelledError:
        journal.transition(intent, "UNKNOWN")
        raise
    except Exception:
        journal.transition(intent, "UNKNOWN")
        reference = journal.get(intent)["reference"]
    try:
        # 即使 POST 逾時、沒有 reference，仍讓對帳器查持倉／掛單／活動。
        result = await asyncio.wait_for(reconcile(reference), timeout=timeout_seconds)
        state = result.get("outcome")
        if state not in ("CONFIRMED", "REJECTED") or reference is None:
            state = "UNKNOWN"
    except asyncio.CancelledError:
        if journal.get(intent)["state"] != "UNKNOWN":
            journal.transition(intent, "UNKNOWN")
        raise
    except Exception:
        state = "UNKNOWN"
    if journal.get(intent)["state"] != state:
        journal.transition(intent, state)
    # 不回傳原始 broker 回應或例外，避免透過一般日誌洩漏私密資料。
    return {"intent_id": intent, "state": state, "entries_enabled": False}
