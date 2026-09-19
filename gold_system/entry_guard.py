"""Demo 初始開倉的獨立再驗證。輸入由 gateway 的權威資料來源提供，不由分析代理提供。"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re

from .core import D, Contract, Direction, Mode, Quality, Quote, RiskPolicy, Signal, Rejected
from .reconciliation import Snapshot, evaluate
from .risk import make_plan


@dataclass(frozen=True)
class EntryContext:
    snapshot: Snapshot
    quote: Quote
    quality: Quality
    contract: Contract
    equity: D
    day_start_equity: D
    realized_daily_pnl: D
    accounting_day: str
    accounting_received_at: datetime
    contract_received_at: datetime
    price_tick: D
    maximum_stop_distance: D
    demo_enabled: bool = False
    contract_verified: bool = False
    strategy_validated: bool = False
    soft_locked: bool = True
    hard_locked: bool = True


class DemoEntryGuard:
    def __init__(self, source, cost_ledger, store, account_hash, policy=None, clock=None):
        if not isinstance(account_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", account_hash):
            raise ValueError("Explicit account fingerprint required")
        self.source, self.cost_ledger, self.store = source, cost_ledger, store
        self.account_hash = account_hash
        self.policy = policy or RiskPolicy()
        p = self.policy
        if (not all(isinstance(v, D) and v.is_finite() for v in
                    (p.risk_fraction, p.normal_margin, p.absolute_margin, p.soft_loss, p.hard_loss, p.minimum_rr, p.slippage))
                or not 0 < p.risk_fraction <= D("0.0025") or not 0 < p.normal_margin <= D("0.20")
                or p.absolute_margin != D("0.70") or p.soft_loss != D("0.03") or p.hard_loss != D("0.10")
                or p.minimum_rr < D("1.5") or p.slippage < 0):
            raise ValueError("Demo policy cannot exceed the approved risk envelope")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def __call__(self, journal, intent):
        def require(condition, reason):
            if not condition:
                raise Rejected(reason)
        try:
            row = journal.get(intent)
            require(row["state"] == "SUBMITTING" and row["account_hash"] == self.account_hash, "GUARD_INTENT_SCOPE")
            context = await self.source()
            now = self.clock()
            require(not self.store.entries_stopped(), "GUARD_OPERATOR_STOP_NEW")
            require(isinstance(context, EntryContext), "GUARD_CONTEXT_MISSING")
            require(context.demo_enabled is True and context.strategy_validated is True, "GUARD_DEMO_NOT_QUALIFIED")
            require(context.contract_verified is True and context.contract.epic == "GOLD", "GUARD_CONTRACT_UNVERIFIED")
            require(context.soft_locked is False and context.hard_locked is False, "GUARD_CIRCUIT_LOCKED")
            require(all(isinstance(v, D) and v.is_finite() for v in
                        (context.equity, context.day_start_equity, context.realized_daily_pnl)), "GUARD_ACCOUNT_VALUES")
            require(context.equity > 0 and context.day_start_equity > 0, "GUARD_ACCOUNT_VALUES")
            require(context.accounting_day == now.astimezone(timezone.utc).date().isoformat(), "GUARD_ACCOUNTING_DAY")
            require(0 <= (now-context.accounting_received_at).total_seconds() <= 2, "GUARD_ACCOUNTING_STALE")
            require(0 <= (now-context.contract_received_at).total_seconds() <= 300, "GUARD_CONTRACT_STALE")
            require(context.realized_daily_pnl > -context.day_start_equity*self.policy.soft_loss, "GUARD_DAILY_LOSS")
            other = [key for key in journal.unresolved() if key != intent]
            other += self.store.unresolved_intents()
            account = evaluate(context.snapshot, now=now, expected_account_hash=self.account_hash,
                               unresolved_intents=other)
            require(account["state"] == "SNAPSHOT_MATCHED", "GUARD_ACCOUNT_NOT_FLAT_AND_RECONCILED")
            budget = self.cost_ledger.status(now)
            require(budget["analysis_available"] is True, "GUARD_BUDGET_UNAVAILABLE")
            request = row["request"]
            require(request["epic"] == "GOLD" and request["direction"] in ("BUY", "SELL"), "GUARD_INSTRUMENT")
            signal = Signal(intent, request["version"], datetime.fromisoformat(request["created"]),
                            datetime.fromisoformat(request["expires"]),
                            Direction.LONG if request["direction"] == "BUY" else Direction.SHORT,
                            Mode(request["mode"]), D(request["stopLevel"]), D(request["profitLevel"]), "JOURNAL_RECHECK")
            quality = replace(context.quality, budget_available=context.quality.budget_available and budget["analysis_available"])
            plan = make_plan(signal, context.quote, quality, context.contract, self.policy, context.equity, now)
            tick, maximum = context.price_tick, context.maximum_stop_distance
            require(all(isinstance(v, D) and v.is_finite() and v > 0 for v in (tick, maximum)), "GUARD_PRICE_RULES")
            require(signal.stop % tick == 0 and signal.target % tick == 0, "GUARD_PRICE_TICK")
            require(abs(plan.entry-signal.stop) <= maximum and abs(plan.entry-signal.target) <= maximum, "GUARD_MAX_DISTANCE")
            size = D(request["size"])
            require(size.is_finite() and context.contract.minimum*2 <= size <= plan.size, "GUARD_SIZE_EXCEEDS_CURRENT_LIMIT")
            require(size % (context.contract.increment*2) == 0, "GUARD_SIZE_STEP")
            risk, margin = plan.risk*size/plan.size, plan.margin*size/plan.size
            require(risk <= D(request["risk"]) and margin <= D(request["margin"]), "GUARD_ENVELOPE_CHANGED")
            require((plan.entry-D(request["planned_entry"]))*signal.direction.sign <= self.policy.slippage,
                    "GUARD_ENTRY_MOVED")
            self.store.emit("DEMO_ENTRY_GUARD", intent_id=intent, approved=True, risk=risk, margin=margin,
                            observed_at=now, account_hash=self.account_hash)
            return True
        except Rejected as exc:
            self.store.emit("DEMO_ENTRY_GUARD", intent_id=intent, approved=False, reason=str(exc))
            return False
        except Exception:
            # 權威來源不可用、格式錯誤或費用庫故障均封鎖；不把服務例外內容寫入一般日誌。
            self.store.emit("DEMO_ENTRY_GUARD", intent_id=intent, approved=False, reason="GUARD_INPUT_UNAVAILABLE")
            return False
