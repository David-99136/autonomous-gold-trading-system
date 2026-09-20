"""分析呼叫的持久化上限與版本追蹤；預留一次即耗用，不盲目重試。"""
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math


def checksum(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return sha256(raw.encode()).hexdigest()


class AnalysisJournal:
    def __init__(self, store, *, scope, max_calls):
        if not isinstance(scope, str) or not scope or len(scope) > 100 or type(max_calls) is not int or max_calls <= 0:
            raise ValueError("ANALYSIS_BUDGET_INVALID")
        self.db, self.scope = store.db, scope
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS analysis_budgets (
                scope TEXT PRIMARY KEY, max_calls INTEGER NOT NULL, binding TEXT);
            CREATE TABLE IF NOT EXISTS analysis_calls (
                scope TEXT NOT NULL, input_hash TEXT NOT NULL, state TEXT NOT NULL,
                requested_at TEXT NOT NULL, result TEXT,
                PRIMARY KEY(scope,input_hash));
        ''')
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT max_calls FROM analysis_budgets WHERE scope=?", (scope,)).fetchone()
            if old and old[0] != max_calls:
                raise ValueError("ANALYSIS_BUDGET_CHANGE_REQUIRES_APPROVAL")
            if not old:
                self.db.execute("INSERT INTO analysis_budgets VALUES(?,?,NULL)", (scope, max_calls))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @property
    def used(self):
        return self.db.execute("SELECT count(*) FROM analysis_calls WHERE scope=?", (self.scope,)).fetchone()[0]

    def reserve(self, *, frame, candidates, evidence, model, prompt_version):
        versions = sorted(set(s.version for s in candidates))
        if len(versions) != 1 or not versions[0] or len({s.signal_id for s in candidates}) != len(candidates):
            raise ValueError("ANALYSIS_CANDIDATES_INVALID")
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("ANALYSIS_MODEL_INVALID")
        binding = json.dumps(dict(strategy=versions[0], model=model, prompt=prompt_version), sort_keys=True)
        # 時鐘不是 key 的額外鹽值：同一輸入重送不得靠新呼叫時間繞過去重。
        key = checksum(dict(binding=binding, candidates=[asdict(s) for s in candidates], evidence=evidence,
                            frame=asdict(frame) if is_dataclass(frame) else frame))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            limit, old_binding = self.db.execute("SELECT max_calls,binding FROM analysis_budgets WHERE scope=?", (self.scope,)).fetchone()
            if old_binding is not None and old_binding != binding:
                raise ValueError("ANALYSIS_VERSION_BINDING_CHANGED")
            duplicate = self.db.execute("SELECT 1 FROM analysis_calls WHERE scope=? AND input_hash=?", (self.scope, key)).fetchone()
            if duplicate or self.used >= limit:
                self.db.rollback()
                return None
            self.db.execute("UPDATE analysis_budgets SET binding=? WHERE scope=?", (binding, self.scope))
            self.db.execute("INSERT INTO analysis_calls VALUES(?,?,'RESERVED',?,NULL)",
                            (self.scope, key, datetime.now(timezone.utc).isoformat()))
            self.db.commit()
            return key
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, key, result, *, elapsed_seconds):
        usage = result.get("usage")
        names = ("input_tokens", "cached_input_tokens", "output_tokens")
        if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in names)
                or usage["cached_input_tokens"] > usage["input_tokens"]
                or type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
            raise ValueError("ANALYSIS_RESULT_METRICS_INVALID")
        # 僅持久化白名單摘要。真正選擇是否合法仍由 runner 及 bridge 驗證。
        safe = dict(output_hash=checksum(result), usage={k: usage[k] for k in names},
                    elapsed_seconds=elapsed_seconds, actual_usd=None, confidence_calibrated=False)
        with self.db:
            changed = self.db.execute("UPDATE analysis_calls SET state='COMPLETED',result=? WHERE scope=? AND input_hash=? AND state='RESERVED'",
                                      (json.dumps(safe, sort_keys=True), self.scope, key)).rowcount
            if not changed:
                raise ValueError("ANALYSIS_RESULT_STATE_CONFLICT")

    def failed(self, key):
        with self.db:
            self.db.execute("UPDATE analysis_calls SET state='UNKNOWN' WHERE scope=? AND input_hash=? AND state='RESERVED'",
                            (self.scope, key))
