"""離線檔案核對：沒有券商、憑證、收棒認證或解鎖能力。

[Author: Antigravity | Date: 2026-09-27]
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import stat
from uuid import uuid4

from .history_quality import HistoryQualityMonitor
from .store import Store


MAX_INPUT_BYTES = 2 * 1024 * 1024


def _local_path(value):
    path = Path(value)
    # 不接受 UNC／裝置路徑或 URL；本入口只讀本機一般檔案。
    if path.as_posix().startswith("//") or "://" in str(value):
        raise ValueError("LOCAL_PATH_REQUIRED")
    return path


def _timestamp(text):
    value = datetime.fromisoformat(text)
    if value.utcoffset() is None:
        raise ValueError("OFFSET_REQUIRED")
    return value.astimezone(timezone.utc)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("NONFINITE_JSON")


def _persist_failure(store, result):
    """錯誤摘要與新倉鎖一起提交；失敗時不可宣稱鎖已成功落盤。"""
    store.db.rollback()
    store.db.execute("BEGIN IMMEDIATE")
    try:
        store.db.execute("INSERT OR REPLACE INTO state VALUES('market_data_blocked','true')")
        store.db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), "HISTORY_IMPORT_FAILED",
                          json.dumps(result, sort_keys=True)))
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise


def audit_file(database, source, *, start, end, received_at, create_diagnostic=False):
    """回傳 (白名單摘要, exit code)。0=未驗證觀測，2=資料封鎖，1=操作失敗。

    接收時間和來源均由呼叫者提供，不是可信券商回執。既有資料庫預設必須存在。
    所有原始例外均抑制，避免路徑、JSON 或資料庫中的秘密出現在終端。
    """
    imported_at = datetime.now(timezone.utc)
    base = dict(import_id=uuid4().hex, imported_at=imported_at.isoformat(),
                received_at_provenance="CALLER_SUPPLIED", source_verified=False,
                scope="CALLER_SUPPLIED_DEMO_GOLD_MINUTE", normalization_version="history-price-v1",
                entries_enabled=False, closure_verified=False)
    store = None
    stage = "HISTORY_DATABASE_OPEN_FAILED"
    try:
        path = _local_path(database)
        if create_diagnostic:
            # 必須是未使用的新檔案；不可覆蓋現有 DB。父目錄亦須由使用者先建立。
            with path.open("xb"):
                pass
            store = Store(path)
            store.set("history_database_purpose", "diagnostic")
        else:
            store = Store(path, must_exist=True)
        base["database_purpose"] = ("diagnostic" if store.get("history_database_purpose") == "diagnostic"
                                    else "existing_unverified")
        stage = "HISTORY_TIME_INVALID"
        start_time, end_time, received = map(_timestamp, (start, end, received_at))
        if received > imported_at:
            raise ValueError("FUTURE_RECEIPT")
        base.update(start=start_time.isoformat(), end=end_time.isoformat(), received_at=received.isoformat())
        stage = "HISTORY_INPUT_READ_FAILED"
        source_path = _local_path(source)
        if not stat.S_ISREG(source_path.stat().st_mode):
            raise ValueError("REGULAR_FILE_REQUIRED")
        with source_path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        stage = "HISTORY_INPUT_TOO_LARGE"
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("INPUT_LIMIT")
        # 只留來源摘要；不保存檔名、任意 JSON 欄位或認證資訊。
        base["source_sha256"] = sha256(raw).hexdigest()
        stage = "HISTORY_JSON_INVALID"
        response = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal,
                              parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        stage = "HISTORY_PERSISTENCE_FAILED"
        store.emit("HISTORY_IMPORT_STARTED", **base)
        result = HistoryQualityMonitor(store).observe(
            response, start=start_time, end=end_time, received_at=received)
        result = base | result | {"completed_at": datetime.now(timezone.utc).isoformat()}
        store.emit("HISTORY_IMPORT_FINISHED", **result)
        return result, 2 if result["status"] == "RECOVERY_REQUIRED" else 0
    except Exception:
        result = base | dict(status="FAILED", reasons=[stage], lock_persisted=None,
                             completed_at=datetime.now(timezone.utc).isoformat())
        if store is not None:
            try:
                _persist_failure(store, result | {"lock_persisted": True})
                result["lock_persisted"] = True
            except Exception:
                result["reasons"].append("HISTORY_LOCK_WRITE_FAILED")
        return result, 1
    finally:
        if store is not None:
            store.close()
