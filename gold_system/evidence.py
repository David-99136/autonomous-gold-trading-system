"""量化發布證據的不可覆寫版本庫；不接新聞供應商，也不賦予方向或交易權限。"""
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlsplit

from .core import D


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evidence timestamps require timezone offsets")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuantitativeEvidence:
    evidence_id: str
    series: str
    vintage: str
    source: str
    observed_at: datetime
    released_at: datetime
    received_at: datetime
    actual: D
    unit: str
    raw_sha256: str

    def payload(self):
        data = asdict(self)
        for key in ("evidence_id", "series", "vintage", "unit"):
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError("Missing quantitative evidence identity or unit")
        url = urlsplit(self.source)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Evidence requires a credential-free HTTPS source")
        if not isinstance(self.actual, D) or not self.actual.is_finite():
            raise ValueError("Actual value must be a finite Decimal")
        if (not isinstance(self.raw_sha256, str) or len(self.raw_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.raw_sha256)):
            raise ValueError("Raw evidence checksum required")
        times = [aware(getattr(self, k)) for k in ("observed_at", "released_at", "received_at")]
        if not times[0] <= times[1] <= times[2]:
            raise ValueError("Actual observation/release/receive order invalid")
        for key, value in zip(("observed_at", "released_at", "received_at"), times):
            data[key] = value.isoformat()
        data["actual"] = str(self.actual)
        return data


class EvidenceArchive:
    def __init__(self, store):
        self.db = store.db
        self.db.execute("CREATE TABLE IF NOT EXISTS quantitative_evidence "
                        "(id TEXT PRIMARY KEY, series TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL)")
        self.db.commit()

    def append(self, evidence):
        payload = evidence.payload()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        checksum = sha256(raw.encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT checksum FROM quantitative_evidence WHERE id=?",
                                    (evidence.evidence_id,)).fetchone()
            if prior:
                if prior[0] != checksum:
                    raise ValueError("Evidence ID content conflict; revisions require a new ID")
                self.db.commit()
                return False
            self.db.execute("INSERT INTO quantitative_evidence VALUES(?,?,?,?)",
                            (evidence.evidence_id, evidence.series, raw, checksum))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def as_of(self, series, cutoff, maximum_release_age):
        """同時限制發布與本系統接收時間；今天下載的舊數值不冒充當時已收到。

        新鮮度須由呼叫端明訂，不能把 COT 的有效期套用在即時報價。
        """
        cutoff = aware(cutoff)
        if not isinstance(maximum_release_age, timedelta) or maximum_release_age <= timedelta(0):
            raise ValueError("Explicit positive evidence age required")
        candidates = []
        for raw, checksum in self.db.execute("SELECT payload,checksum FROM quantitative_evidence WHERE series=?", (series,)):
            if sha256(raw.encode()).hexdigest() != checksum:
                raise ValueError("Evidence archive checksum mismatch")
            data = json.loads(raw)
            for key in ("observed_at", "released_at", "received_at"):
                data[key] = datetime.fromisoformat(data[key])
            data["actual"] = D(data["actual"])
            evidence = QuantitativeEvidence(**data)
            evidence.payload()
            if (evidence.received_at <= cutoff and evidence.released_at <= cutoff
                    and cutoff-evidence.released_at <= maximum_release_age):
                candidates.append(evidence)
        if not candidates:
            return None
        latest = max(e.released_at for e in candidates)
        latest_candidates = [e for e in candidates if e.released_at == latest]
        # 相同發布時間的不同數值或單位不能靠 ID 字母順序決定真實版本。
        if len({(e.actual, e.unit, e.observed_at) for e in latest_candidates}) != 1:
            raise ValueError("Ambiguous evidence vintage at identical release time")
        return max(latest_candidates, key=lambda e: (e.received_at, e.evidence_id))

    def context(self, required, optional_weights, maximum_ages, cutoff, completeness_version):
        """完整性由當時可用的版本計算，而不是以今天資料欄位是否存在來計算。"""
        required = tuple(required)
        if (not required or len(set(required)) != len(required) or not optional_weights
                or set(required) & set(optional_weights)
                or not isinstance(completeness_version, str) or not completeness_version.strip()
                or any(not isinstance(k, str) or not k for k in (*required, *optional_weights))
                or any(not isinstance(w, D) or not w.is_finite() or w <= 0 for w in optional_weights.values())):
            raise ValueError("Explicit disjoint required/weighted optional evidence and version required")
        selected = {name: self.as_of(name, cutoff, maximum_ages[name]) for name in (*required, *optional_weights)}
        missing_required = [name for name in required if selected[name] is None]
        missing_weight = sum((weight for name, weight in optional_weights.items() if selected[name] is None), D(0))
        ratio = missing_weight/sum(optional_weights.values(), D(0))
        return {"evidence_eligible": not missing_required and ratio < D("0.4"),
                "as_of": aware(cutoff).isoformat(), "completeness_version": completeness_version,
                "missing_required": missing_required, "weighted_missingness": ratio,
                "records": {name: value.payload() for name, value in selected.items() if value is not None},
                "entries_enabled": False}
