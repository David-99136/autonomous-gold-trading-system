"""採樣計畫與排他診斷檔案；無網路、無憑證相依。

[Author: Antigravity | Date: 2026-09-27]
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile


class SampleError(RuntimeError):
    """僅由本模組產生固定代碼；不放入外部錯誤字串。"""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


def utc(value):
    if not isinstance(value, str) or len(value) > 40:
        raise SampleError("PLAN_TIME_INVALID")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise SampleError("PLAN_TIME_INVALID") from None


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(_):
        raise ValueError()
    from decimal import Decimal
    return json.loads(raw, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=constant)


def validate_plan(value):
    """所有額外欄位均拒絕：計畫不能塞入 URL、headers 或訂單。"""
    keys = {"version", "environment", "epic", "start", "end", "queries"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int
            or value["version"] != 1 or value["environment"] != "DEMO" or value["epic"] != "GOLD"):
        raise SampleError("PLAN_SCOPE_INVALID")
    start, end = utc(value["start"]), utc(value["end"])
    if not timedelta(seconds=15) <= end - start <= timedelta(minutes=75):
        raise SampleError("PLAN_DURATION_INVALID")
    rows = value["queries"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 240:
        raise SampleError("PLAN_QUERY_LIMIT")
    normalized, previous = [], None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"at", "from", "to", "resolution", "max"}:
            raise SampleError("PLAN_QUERY_INVALID")
        at, left, right = (utc(row[k]) for k in ("at", "from", "to"))
        if (row["resolution"] not in ("MINUTE", "MINUTE_5", "HOUR")
                or type(row["max"]) is not int or not 1 <= row["max"] <= 1000
                or not left <= right <= at or right-left > timedelta(minutes=999)
                or any(t.second or t.microsecond for t in (left, right))
                or not start + timedelta(seconds=10) <= at < end-timedelta(seconds=5)
                or (previous is not None and at-previous < timedelta(seconds=5))):
            raise SampleError("PLAN_QUERY_INVALID")
        previous = at
        normalized.append(dict(at=at.isoformat(), **{"from": left.isoformat(), "to": right.isoformat()},
                               resolution=row["resolution"], max=row["max"]))
    return dict(version=1, environment="DEMO", epic="GOLD", start=start.isoformat(),
                end=end.isoformat(), queries=normalized)


def read_plan(path):
    try:
        path = Path(path)
        if path.as_posix().startswith("//") or "://" in str(path) or not path.is_file():
            raise ValueError()
        with path.open("rb") as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError()
        return validate_plan(strict_json(raw))
    except SampleError:
        raise
    except Exception:
        raise SampleError("PLAN_READ_FAILED") from None


class Archive:
    """只建立 root 下的一層新目錄。CLI 固定 root 為專案 runtime。"""
    def __init__(self, root, name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise SampleError("ARCHIVE_NAME_INVALID")
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root or self.root.is_symlink():
            raise SampleError("ARCHIVE_ROOT_INVALID")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / name
        self.path.mkdir(exist_ok=False)
        self.size = 0

    def put(self, name, value):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.json", name):
            raise SampleError("ARCHIVE_NAME_INVALID")
        raw = encoded(value)
        if self.size + len(raw) > 64 * 1024 * 1024:
            raise SampleError("ARCHIVE_SIZE_LIMIT")
        with (self.path / name).open("xb") as stream:
            self.size += len(raw)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return sha256(raw).hexdigest()


@contextmanager
def diagnostic_lease(root):
    """全專案排他，較單帳戶嚴格；崩潰留下的租約不自動刪除。"""
    path = Path(root) / "history-sampler.lease"
    try:
        stream = path.open("xb")
    except FileExistsError:
        raise SampleError("DIAGNOSTIC_LEASE_BUSY") from None
    try:
        owner = os.fstat(stream.fileno())
        yield
    finally:
        stream.close()
        # 只清理由本次取得的檔案，不觸碰被替換的租約。
        if path.exists() and os.path.samestat(owner, path.stat()):
            path.unlink()


@contextmanager
def account_lease(identifier):
    """同一 Windows 使用者暫存區跨工作副本排他；不保證其他主機／OS 使用者。"""
    if not isinstance(identifier, str) or not identifier.strip():
        raise SampleError("ACCOUNT_LEASE_ID_INVALID")
    key = sha256(identifier.strip().casefold().encode()).hexdigest()
    root = Path(tempfile.gettempdir()).absolute() / "gold-history-sampler-leases" / key
    if root.resolve() != root:
        raise SampleError("ACCOUNT_LEASE_PATH_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    with diagnostic_lease(root):
        yield
