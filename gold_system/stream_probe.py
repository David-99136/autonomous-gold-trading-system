"""有期限的唯讀行情檢查；只保存明確允許的價格欄位及固定狀態碼。"""
import asyncio
import json
import time
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path

from .async_capital import AsyncCapitalDemo
from .capital import load_credentials
from .streaming import FeedError
from .clock_sync import ClockError, synchronize


async def capture_quotes(output, seconds, *, epic="GOLD"):
    if epic not in ("GOLD", "ETHUSD"):
        raise ValueError("Unsupported diagnostic instrument")
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError("Invalid probe duration")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    count, events, outcome = 0, [], "NOT_STARTED"
    with path.open("x", encoding="utf-8") as stream:
        def record(kind, **values):
            stream.write(json.dumps(dict(received_at=datetime.now(timezone.utc).isoformat(),
                                         kind=kind, **values)) + "\n")
            stream.flush()

        def event(kind):
            events.append(kind)
            record(kind)

        client = AsyncCapitalDemo()
        try:
            await client.login(load_credentials())
            broker_clock = await synchronize(client)
            record("CLOCK_ESTIMATE", offset_ms=broker_clock.offset_ms,
                   uncertainty_ms=broker_clock.uncertainty_ms)
            async with asyncio.timeout(seconds):
                async with aclosing(client.quotes(on_event=event, epic=epic, clock=lambda: broker_clock.now())) as feed:
                    async for quote in feed:
                        count += 1
                        record("QUOTE", broker_timestamp=quote.timestamp.isoformat(),
                               bid=str(quote.bid), ask=str(quote.ask))
                        if time.monotonic()-broker_clock.monotonic_anchor >= 30:
                            broker_clock = await synchronize(client)
                            record("CLOCK_ESTIMATE", offset_ms=broker_clock.offset_ms,
                                   uncertainty_ms=broker_clock.uncertainty_ms)
            outcome = "STREAM_ENDED"
        except TimeoutError:
            outcome = "DURATION_REACHED"
        except FeedError as exc:
            outcome = str(exc)
        except ClockError as exc:
            outcome = str(exc)
        except Exception:
            outcome = "PROBE_FAILED"  # Vault／HTTP 例外原文可能含敏感內容。
        finally:
            await client.close()
            result = dict(epic=epic, outcome=outcome, subscribed="STREAM_SUBSCRIBED" in events,
                          quote_count=count, entries_enabled=False)
            record("PROBE_RESULT", **result)
    return result
