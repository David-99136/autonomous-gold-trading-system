"""有界、可注入時鐘的證據採樣；完成不代表已收棒／策略合格。

[Author: Antigravity | Date: 2026-09-27]
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
import time
from uuid import uuid4

from .history_quality import _row
from .sampler_plan import SampleError, digest, utc, validate_plan


MAX_REQUESTS = 900


class SystemClock:
    def wall(self):
        return datetime.now(timezone.utc)

    def mono(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def clean_prices(data, query, first):
    """正常數值保留，未知／不合法欄位不抄入證據；錯誤只記列號與代碼。"""
    rows = data.get("prices")
    if not isinstance(rows, list) or not 1 <= len(rows) <= query["max"]:
        return {"prices": [], "faults": [{"code": "PRICE_ROWS_INVALID"}]}
    step = {"MINUTE": 60, "MINUTE_5": 300, "HOUR": 3600}[query["resolution"]]
    safe, faults, previous = [], [], None
    for index, row in enumerate(rows):
        try:
            stamp, fingerprint, reasons = _row(row)
            if (int(stamp.timestamp()) % step or not utc(query["from"]) <= stamp <= utc(query["to"])):
                reasons.add("PRICE_TIMESTAMP_INVALID")
            if previous is not None and (stamp-previous).total_seconds() != step:
                reasons.add("PRICE_SEQUENCE_INVALID")
            previous = stamp
            key = (query["resolution"], stamp.isoformat())
            if key in first and first[key] != fingerprint:
                reasons.add("PRICE_REVISION")
            first.setdefault(key, fingerprint)
            item = {"snapshotTimeUTC": stamp.isoformat()}
            for field in ("openPrice", "highPrice", "lowPrice", "closePrice"):
                item[field] = {side: format(Decimal(str(row[field][side])), "f") for side in ("bid", "ask")}
            safe.append(item)
            faults.extend(dict(row=index, code=reason) for reason in sorted(reasons))
        except Exception:
            faults.append(dict(row=index, code="PRICE_ROW_INVALID"))
    # 不推定券商 from/to 的等號語义；缺頭／缺尾在報告中揭露，不能當作完整行情。
    return dict(prices=safe, faults=faults, endpoint_semantics="UNVERIFIED")


async def capture(plan, reader, archive, clock):
    """唯一執行入口；reader 接受 SESSION/TIME/MARKET/PRICES，無任意 URL 能力。"""
    run_id = uuid4().hex
    count, last_send, estimate, first = 0, None, None, {}
    summary = dict(run_id=run_id, status="FAILED", reason="CAPTURE_FAILED", requests=0,
                   entries_enabled=False, closure_verified=False, qualified=False, completion_recorded=False)
    anchor_wall, anchor_mono = clock.wall(), clock.mono()
    deadline = anchor_mono

    def stamp():
        wall, mono = clock.wall(), clock.mono()
        if (wall.utcoffset() is None or not math.isfinite(mono) or mono < anchor_mono
                or abs((wall-anchor_wall).total_seconds()-(mono-anchor_mono)) > .05):
            raise SampleError("CLOCK_JUMP")
        if mono >= deadline:
            raise SampleError("RUN_DEADLINE")
        return dict(utc=wall.astimezone(timezone.utc).isoformat(), monotonic=mono)

    async def request(kind, query=None, *, send_by=None):
        nonlocal count, last_send
        if count >= MAX_REQUESTS:
            raise SampleError("REQUEST_LIMIT")
        queue = stamp()
        request_id = f"{count+1:04d}"
        receipt = dict(run_id=run_id, request_id=request_id, plan_sha256=summary["plan_sha256"],
                       kind=kind, query=query or {}, queue_start=queue, send_start=None,
                       headers_received=None, body_complete=None, validation_complete=None,
                       http_status=None, body_sha256=None, clock_estimate=None)
        archive.put(f"request-{request_id}-start.json", receipt)
        limit = min(deadline, queue["monotonic"] + (10 if kind == "SESSION" else 5))

        def mark(phase):
            value = stamp()
            if value["monotonic"] >= limit:
                raise SampleError("REQUEST_DEADLINE")
            previous = next((receipt[k] for k in ("body_complete", "headers_received", "send_start")
                             if receipt[k] is not None), queue)
            if value["monotonic"] < previous["monotonic"]:
                raise SampleError("CLOCK_JUMP")
            receipt[phase] = value

        try:
            delay = 0 if last_send is None else max(0, 1-(clock.mono()-last_send))
            if clock.mono()+delay >= limit:
                raise SampleError("REQUEST_DEADLINE")
            await clock.sleep(delay)
            if send_by is not None and clock.wall() > send_by:
                raise SampleError("SCHEDULE_MISSED")
            if kind in ("MARKET", "PRICES"):
                if estimate is None or not 0 <= clock.mono()-estimate["anchor_monotonic"] <= 60:
                    raise SampleError("CLOCK_ESTIMATE_EXPIRED")
                receipt["clock_estimate"] = dict(estimate, age_seconds=clock.mono()-estimate["anchor_monotonic"])
            mark("send_start")
            count += 1
            last_send = clock.mono()
            async with asyncio.timeout(max(0, limit-clock.mono())):
                data = await reader.send(kind, query or {}, receipt, mark)
            stamp()
            if receipt["body_complete"] is None:
                raise SampleError("RECEIPT_INCOMPLETE")
            safe = {}
            if kind == "TIME":
                if type(data.get("serverTime")) is not int:
                    raise SampleError("CLOCK_SAMPLE_INVALID")
                safe = {"serverTime": data["serverTime"]}
            elif kind == "MARKET":
                market = data.get("snapshot", {}).get("marketStatus")
                epic = data.get("instrument", {}).get("epic")
                if market != "TRADEABLE" or epic != "GOLD":
                    raise SampleError("MARKET_UNAVAILABLE")
                safe = dict(epic="GOLD", marketStatus="TRADEABLE")
            elif kind == "PRICES":
                safe = clean_prices(data, query, first)
            mark("validation_complete")
            if safe:
                file = f"request-{request_id}-data.json"
                receipt["data_file"] = file
                receipt["data_sha256"] = archive.put(file, safe)
                receipt["normalization_version"] = "sampler-whitelist-v1"
            receipt["outcome"] = "QUALITY_FAULT" if safe.get("faults") else "OBSERVED_UNVERIFIED"
            archive.put(f"request-{request_id}-complete.json", receipt)
            if safe.get("faults"):
                raise SampleError("PRICE_QUALITY_FAULT")
            return safe, receipt
        except BaseException as exc:
            receipt["outcome"] = "FAILED"
            receipt["error"] = (str(exc) if isinstance(exc, SampleError) else
                                "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "READ_FAILED")
            # 不覆蓋可能已完成的回執；失敗另留檔。失敗落盤再失敗由外層回報未知。
            archive.put(f"request-{request_id}-failed.json", receipt)
            raise

    async def sync():
        nonlocal estimate
        samples, ids = [], []
        for _ in range(3):
            data, receipt = await request("TIME")
            before, after = receipt["send_start"], receipt["body_complete"]
            half = (after["monotonic"]-before["monotonic"])/2
            offset = data["serverTime"] - (utc(before["utc"]).timestamp()+utc(after["utc"]).timestamp())*500
            samples.append(dict(offset_ms=offset, uncertainty_ms=half*1000,
                                anchor_monotonic=after["monotonic"],
                                server_anchor=(datetime.fromtimestamp(data["serverTime"]/1000, timezone.utc)
                                               + timedelta(seconds=half)).isoformat()))
            ids.append(receipt["request_id"])
        estimate = min(samples, key=lambda s: s["uncertainty_ms"])
        if abs(estimate["offset_ms"])+estimate["uncertainty_ms"] > 1000:
            raise SampleError("CLOCK_OFFSET_EXCEEDS_LIMIT")
        estimate = dict(estimate, estimate_id=ids[-1], sample_request_ids=ids)
        archive.put(f"clock-{ids[-1]}.json", estimate)

    try:
        plan = validate_plan(plan)
        summary["plan_sha256"] = digest(plan)
        start, end = utc(plan["start"]), utc(plan["end"])
        if not start <= anchor_wall < end or utc(plan["queries"][0]["at"]) <= anchor_wall:
            raise SampleError("PLAN_NOT_CURRENT")
        deadline = anchor_mono+(end-anchor_wall).total_seconds()
        archive.put("plan.json", plan)
        archive.put("run-start.json", dict(run_id=run_id, plan_sha256=summary["plan_sha256"], started_at=stamp()))
        await request("SESSION")
        await sync()
        await request("MARKET")
        for row in plan["queries"]:
            at = utc(row["at"])
            while True:
                stamp()
                age = clock.mono()-estimate["anchor_monotonic"]
                if age >= 30:
                    await sync()
                    continue
                remaining = (at-clock.wall()).total_seconds()
                if remaining <= 0:
                    break
                await clock.sleep(min(remaining, 30-age))
            if clock.wall() > at+timedelta(seconds=1):
                raise SampleError("SCHEDULE_MISSED")
            await request("PRICES", {k: row[k] for k in ("from", "to", "resolution", "max")},
                          send_by=at+timedelta(seconds=1))
        stamp()
        summary.update(status="COMPLETED_UNVERIFIED", reason="PLAN_FINISHED")
    except asyncio.CancelledError:
        summary["reason"] = "CANCELLED"
    except SampleError as exc:
        summary["reason"] = str(exc)
    except Exception:
        summary["reason"] = "CAPTURE_FAILED"
    finally:
        try:
            await reader.close()
        except Exception:
            summary.update(status="FAILED", reason="READER_CLOSE_FAILED")
        summary["requests"] = count
        try:
            archive.put("run-result.json", summary | {"completion_recorded": True})
            summary["completion_recorded"] = True
        except Exception:
            summary.update(status="FAILED", reason="ARCHIVE_WRITE_FAILED", completion_recorded=False)
    return summary
