"""保存券商原始 bid/ask 歷史及下載覆蓋率；不替缺失資料補值。"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import json
import re


def utc(value):
    parsed = datetime.fromisoformat(value)
    # API 的 snapshotTimeUTC 沒有 offset，欄位語義本身明確為 UTC。
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def download(client, epic, start, end, output):
    if epic != "GOLD" or start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("Need GOLD and an increasing timezone-aware range")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cursor, collected, pages = start, {}, []
    # 每段最多 900 分鐘，小於官方每次 1000 筆，含端點重疊後仍有餘裕。
    while cursor < end:
        until = min(end, cursor + timedelta(minutes=900))
        response = client.prices(epic, "MINUTE", cursor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                                 until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
        received = datetime.now(timezone.utc).isoformat()
        if not isinstance(response.get("prices"), list):
            raise ValueError("History response missing prices")
        raw = json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
        path = output / f"page-{len(pages):04d}.json"
        path.write_bytes(raw)
        pages.append({"file": path.name, "sha256": sha256(raw).hexdigest(), "received_at": received,
                      "from": cursor.isoformat(), "to": until.isoformat(), "count": len(response["prices"])})
        for row in response["prices"]:
            timestamp = utc(row["snapshotTimeUTC"])
            if not cursor <= timestamp <= until:
                raise ValueError("History outside requested page")
            if start <= timestamp < end:
                key = timestamp.isoformat()
                if key in collected and collected[key] != row:
                    raise ValueError("Conflicting history at page boundary")
                collected[key] = row
        cursor = until
    stamps = sorted(collected)
    gaps = [{"after": a, "before": b} for a, b in zip(stamps, stamps[1:]) if utc(b)-utc(a) > timedelta(minutes=1)]
    manifest = {"source": "CAPITAL_COM_DEMO_API", "epic": epic, "resolution": "MINUTE",
                "requested_from": start.isoformat(), "requested_to": end.isoformat(),
                "rows": len(stamps), "first": stamps[0] if stamps else None,
                "last": stamps[-1] if stamps else None, "gaps_unclassified": gaps,
                "pages": pages, "qualified": False,
                "note": "Timestamps are raw broker timestamps; bar open/close semantics require validation."}
    (output / "prices.json").write_text(json.dumps([collected[k] for k in stamps], ensure_ascii=False), encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def audit_download(directory):
    """離線重建下載檔案並核對完整性；雜湊不是券商簽章，亦非時間語义證明。"""
    root = Path(directory).resolve(strict=True)
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("source") != "CAPITAL_COM_DEMO_API" or manifest.get("epic") != "GOLD"
            or manifest.get("resolution") != "MINUTE"):
        raise ValueError("Unsupported history manifest scope")
    start, end = utc(manifest["requested_from"]), utc(manifest["requested_to"])
    if end <= start:
        raise ValueError("Invalid history range")
    collected, cursor = {}, start
    for index, page in enumerate(manifest["pages"]):
        filename = page["file"]
        if not isinstance(filename, str) or filename != f"page-{index:04d}.json":
            raise ValueError("Unexpected history page path or sequence")
        path = (root/filename).resolve(strict=True)
        if path.parent != root:
            raise ValueError("History page escapes capture directory")
        raw = path.read_bytes()
        expected_hash = page["sha256"]
        if (not isinstance(expected_hash, str) or not re.fullmatch("[a-f0-9]{64}", expected_hash)
                or sha256(raw).hexdigest() != expected_hash):
            raise ValueError("History page checksum mismatch")
        left, right = utc(page["from"]), utc(page["to"])
        if left != cursor or not left < right <= end:
            raise ValueError("History page range is incomplete or overlapping")
        rows = json.loads(raw)["prices"]
        if not isinstance(rows, list) or type(page["count"]) is not int or len(rows) != page["count"]:
            raise ValueError("History page count mismatch")
        for row in rows:
            timestamp = utc(row["snapshotTimeUTC"])
            if timestamp.second or timestamp.microsecond or not left <= timestamp <= right:
                raise ValueError("Invalid history minute timestamp")
            if start <= timestamp < end:
                key = timestamp.isoformat()
                if key in collected and collected[key] != row:
                    raise ValueError("Conflicting history rows")
                collected[key] = row
        cursor = right
    if cursor != end:
        raise ValueError("History pages do not cover requested range")
    stamps = sorted(collected)
    rebuilt = [collected[k] for k in stamps]
    if json.loads((root/"prices.json").read_text(encoding="utf-8")) != rebuilt:
        raise ValueError("Consolidated prices differ from source pages")
    gaps = [{"after": a, "before": b} for a, b in zip(stamps, stamps[1:]) if utc(b)-utc(a) > timedelta(minutes=1)]
    if (type(manifest["rows"]) is not int or manifest["rows"] != len(stamps)
            or manifest["first"] != (stamps[0] if stamps else None)
            or manifest["last"] != (stamps[-1] if stamps else None)
            or manifest["gaps_unclassified"] != gaps):
        raise ValueError("History coverage manifest mismatch")
    return {"integrity": "INTERNALLY_CONSISTENT", "rows": len(stamps), "pages": len(manifest["pages"]),
            "gaps_unclassified": gaps, "qualified": False,
            "limitations": ["NOT_BROKER_SIGNED", "BAR_TIMESTAMP_SEMANTICS_UNVERIFIED",
                            "NO_OHLC_OR_POINT_IN_TIME_QUALITY_VALIDATION"]}
