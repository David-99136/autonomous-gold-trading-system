"""短期券商時鐘估計；保留網路往返不確定度，不修改作業系統時間。"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


class ClockError(RuntimeError):
    """固定時鐘故障碼，可安全用於診斷報告。"""


@dataclass(frozen=True)
class BrokerClock:
    server_anchor: datetime
    monotonic_anchor: float
    offset_ms: float
    uncertainty_ms: float

    def now(self):
        elapsed = time.monotonic() - self.monotonic_anchor
        if not 0 <= elapsed <= 60:
            raise ClockError("CLOCK_ESTIMATE_EXPIRED")
        return self.server_anchor + timedelta(seconds=elapsed)


async def synchronize(client):
    samples = []
    for _ in range(3):
        before, start = time.time(), time.monotonic()
        reply, _ = await client._request("GET", "/time")
        end, after = time.monotonic(), time.time()
        if type(reply.get("serverTime")) is not int or abs((after-before)-(end-start)) > .05:
            raise ClockError("CLOCK_SAMPLE_INVALID")
        half = (end-start)/2
        offset = reply["serverTime"] - (before+after)*500
        anchor = datetime.fromtimestamp(reply["serverTime"]/1000, timezone.utc) + timedelta(seconds=half)
        samples.append(BrokerClock(anchor, end, offset, half*1000))
    result = min(samples, key=lambda value: value.uncertainty_ms)
    # 偏差與不確定度合計必須在規格的一秒內；不能任意放寬報價新鮮度。
    if abs(result.offset_ms) + result.uncertainty_ms > 1000:
        raise ClockError("CLOCK_OFFSET_EXCEEDS_LIMIT")
    return result
