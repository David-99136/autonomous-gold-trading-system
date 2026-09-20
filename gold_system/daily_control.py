"""Demo 每日已實現損失協調器。永不自行解鎖；回呼需由正式 Gateway 接線。"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .accounting import window
from .reporting import timestamp


class DailyControl:
    def __init__(self, accounting, *, session_boundary, clock=None):
        self.accounting, self.db = accounting, accounting.db
        # 必須與此帳戶的正式 Gateway 共用；私人新建鎖不能隔離其他送單程序。
        self.session_boundary = session_boundary
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS daily_control (
                account_hash TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                opening_equity TEXT NOT NULL, net_pnl TEXT NOT NULL,
                soft INTEGER NOT NULL, hard INTEGER NOT NULL, phase TEXT NOT NULL,
                PRIMARY KEY(account_hash,start));
        ''')
        self.db.commit()

    def _block_entries(self):
        # 只操作自己的持久化鎖，不覆寫操作者的 stop-new。既有 Engine／Guard 已讀此鎖。
        self.db.execute("INSERT OR REPLACE INTO state VALUES('daily_new_entries_blocked','true')")

    def _event(self, kind, **fields):
        self.db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                        (self.clock().isoformat(), kind, json.dumps(fields, sort_keys=True)))

    def observe(self, *, account_hash, start, end):
        """只接受已對帳 Demo receipt；截止時間需在 2 秒內，不讀浮動盈虧。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            start, end = window("DEMO", account_hash, start, end)
            age = (self.clock() - timestamp(end)).total_seconds()
            if not 0 <= age <= 2:
                raise ValueError("DAILY_ACCOUNTING_STALE")
            receipt = self.accounting._verified_period("DEMO", account_hash, start, end)
            old = self.db.execute("SELECT end,opening_equity,soft,hard,phase FROM daily_control WHERE account_hash=? AND start=?",
                                  (account_hash, start)).fetchone()
            equity, net = Decimal(receipt["opening_equity"]), Decimal(receipt["net_pnl"])
            if old and (end < old[0] or equity != Decimal(old[1])):
                raise ValueError("DAILY_BOUNDARY_OR_EQUITY_CONFLICT")
            inherited = self.db.execute("SELECT 1 FROM daily_control WHERE account_hash=? AND hard=1", (account_hash,)).fetchone()
            # 保留 precision 以免在剛好 3%／10% 的邊界被捨入。
            with localcontext() as ctx:
                ctx.prec = 80
                soft = bool(old and old[2]) or net <= -equity * Decimal("0.03")
                hard = bool(inherited) or net <= -equity * Decimal("0.10")
            phase = old[4] if old else "MONITORING"
            if hard:
                self._block_entries()
                if old and old[3]:
                    pass  # 不因再看到帳務回報而重跑已開始的換 session 流程。
                elif inherited:
                    phase = "INHERITED_HARD_LOCK"
                else:
                    phase = "PENDING_REDUCTION"
            elif soft:
                self._block_entries()
                phase = "SOFT_LOCKED"
            self.db.execute("INSERT OR REPLACE INTO daily_control VALUES(?,?,?,?,?,?,?,?)",
                            (account_hash, start, end, str(equity), str(net), int(soft), int(hard), phase))
            if old is None or bool(old[2]) != soft or bool(old[3]) != hard:
                self._event("DAILY_RISK_STATE", soft=soft, hard=hard, phase=phase)
            self.db.commit()
            return dict(soft=soft, hard=hard, phase=phase, entries_enabled=False)
        except Exception:
            self.db.rollback()
            with self.db:
                self._block_entries()
                self._event("DAILY_ACCOUNTING_UNVERIFIED")
            # 不輸出 statement、帳號、回呼例外等原文。
            return dict(phase="ACCOUNTING_UNVERIFIED", entries_enabled=False)

    def _transition(self, account_hash, start, expected, phase):
        with self.db:
            changed = self.db.execute("UPDATE daily_control SET phase=? WHERE account_hash=? AND start=? AND phase=? AND hard=1",
                                       (phase, account_hash, start, expected)).rowcount
            if changed:
                self._event("DAILY_RECONCILIATION_PHASE", phase=phase)
        return bool(changed)

    def _flat_same_account(self, report, account_hash):
        try:
            valid = (isinstance(report, dict) and report.get("state") == "SNAPSHOT_MATCHED"
                     and report.get("account_hash") == account_hash and report.get("reasons") == []
                     and type(report.get("position_count")) is int and report["position_count"] == 0
                     and type(report.get("working_order_count")) is int and report["working_order_count"] == 0
                     and report.get("entries_enabled") is False
                     and 0 <= (self.clock() - timestamp(report["received_at"])).total_seconds() <= 2
                     and type(report.get("elapsed_seconds")) in (int, float)
                     and 0 <= report["elapsed_seconds"] <= 2)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("DAILY_EXPOSURE_UNVERIFIED")

    async def reconcile_hard_limit(self, *, account_hash, start, reduce_and_verify,
                                   reauthenticate, reconcile):
        """回呼：先持久化減風險意圖並驗證空倉，再登入、對帳；不提供直接訂單實作。"""
        start = timestamp(start).astimezone(timezone.utc).isoformat()
        if not self._transition(account_hash, start, "PENDING_REDUCTION", "REDUCING"):
            # 另一 worker／重啟後的進行中狀態由恢復器核對，不能再次執行。
            return dict(phase="NOT_CLAIMED", entries_enabled=False)
        phase = "REDUCING"
        try:
            # 單一帳戶 session 邊界需由 Gateway 共用；任何等待/回呼都有上限。
            async with asyncio.timeout(30):
                async with self.session_boundary:
                    async with asyncio.timeout(10):
                        reduced = await reduce_and_verify()
                    self._flat_same_account(reduced, account_hash)
                    if not self._transition(account_hash, start, phase, "REAUTHENTICATING"):
                        raise ValueError("DAILY_STATE_CONFLICT")
                    phase = "REAUTHENTICATING"
                    async with asyncio.timeout(10):
                        await reauthenticate()
                    if not self._transition(account_hash, start, phase, "RECONCILING"):
                        raise ValueError("DAILY_STATE_CONFLICT")
                    phase = "RECONCILING"
                    async with asyncio.timeout(5):
                        fresh = await reconcile()
                    self._flat_same_account(fresh, account_hash)
                    if not self._transition(account_hash, start, phase, "COMPLETE_HARD_LOCKED"):
                        raise ValueError("DAILY_STATE_CONFLICT")
            return dict(phase="COMPLETE_HARD_LOCKED", entries_enabled=False)
        except BaseException as exc:
            self._transition(account_hash, start, phase, "RECOVERY_REQUIRED")
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return dict(phase="RECOVERY_REQUIRED", entries_enabled=False)
