"""GOLD 報價串流：只訂閱行情；憑證留在 Gateway，不傳給分析程序。"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from websockets.asyncio.client import connect

from .core import Quote

STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"


class NoRedirectConnect(connect):
    # 固定接收認證資料的主機；拒絕 WebSocket 握手重新導向。
    def process_redirect(self, exc):
        return exc


class FeedError(RuntimeError):
    """只包含固定錯誤代碼，不能包含原始網路回應或認證資訊。"""


def decode(raw):
    try:
        if not isinstance(raw, (str, bytes)) or len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw, parse_float=Decimal)
        if not isinstance(value, dict) or value.get("status") != "OK":
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise FeedError("STREAM_INVALID_MESSAGE") from None


def parse_quote(message, received_at, previous=None, *, epic="GOLD"):
    """券商毫秒時間戳保留原值；不能用接收時間把舊行情偽裝成新行情。"""
    try:
        data = message["payload"]
        if (epic not in ("GOLD", "ETHUSD") or message["destination"] != "quote" or data["epic"] != epic
                or data["product"] != "CFD" or type(data["timestamp"]) is not int):
            raise ValueError()
        stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=data["timestamp"])
        result = Quote(stamp, Decimal(str(data["bid"])), Decimal(str(data["ofr"])))
        if not result.valid() or received_at.tzinfo is None:
            raise ValueError()
        if not 0 <= (received_at-stamp).total_seconds() <= 2:
            raise FeedError("STREAM_STALE_OR_FUTURE_QUOTE")
        if previous is not None and stamp <= previous:
            raise FeedError("STREAM_UNORDERED_QUOTE")
        return result
    except FeedError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError):
        raise FeedError("STREAM_INVALID_QUOTE") from None


async def gold_quotes(headers, *, on_event=None, connector=NoRedirectConnect,
                      clock=None, silence_timeout=2, epic="GOLD"):
    """一個連線世代；無行情兩秒即結束，不自動重連或解除進場鎖。

    呼叫者必須關閉 iterator。訂閱成功只代表通道正常，不代表市場可交易。
    即使不斷收到 ping，也不能延長最後一筆行情的存活期限。
    """
    if epic not in ("GOLD", "ETHUSD"):
        raise ValueError("Unsupported diagnostic instrument")
    if not 0 < silence_timeout <= 2:
        raise ValueError("Quote silence timeout must be in (0, 2]")
    if any(not isinstance(headers.get(k), str) or not headers[k]
           for k in ("CST", "X-SECURITY-TOKEN")):
        raise FeedError("STREAM_NOT_AUTHENTICATED")
    clock = clock or (lambda: datetime.now(timezone.utc))
    emit = on_event or (lambda event: None)
    auth = {"cst": headers["CST"], "securityToken": headers["X-SECURITY-TOKEN"]}
    try:
        async with connector(STREAM_URL, proxy=None, open_timeout=10, close_timeout=2,
                             max_size=65536, max_queue=4, compression=None) as ws:
            await asyncio.wait_for(ws.send(json.dumps(dict(auth, destination="marketData.subscribe",
                correlationId="gold-1", payload={"epics": [epic]}))), 2)
            ack = decode(await asyncio.wait_for(ws.recv(), 5))
            if (ack.get("destination") != "marketData.subscribe" or ack.get("correlationId") != "gold-1"
                    or ack.get("payload", {}).get("subscriptions", {}).get(epic) != "PROCESSED"):
                raise FeedError("STREAM_SUBSCRIPTION_REJECTED")
            emit("STREAM_SUBSCRIBED")
            loop = asyncio.get_running_loop()
            last_quote, last_ping, previous = loop.time(), loop.time(), None
            while True:
                remaining = silence_timeout - (loop.time()-last_quote)
                if remaining <= 0:
                    raise FeedError("STREAM_QUOTE_TIMEOUT")
                try:
                    message = decode(await asyncio.wait_for(ws.recv(), remaining))
                except TimeoutError:
                    raise FeedError("STREAM_QUOTE_TIMEOUT") from None
                if message.get("destination") == "quote":
                    value = parse_quote(message, clock(), previous, epic=epic)
                    previous, last_quote = value.timestamp, loop.time()
                    yield value
                elif message.get("destination") != "ping":
                    raise FeedError("STREAM_UNEXPECTED_MESSAGE")
                if loop.time()-last_ping >= 300:
                    await asyncio.wait_for(ws.send(json.dumps(dict(auth, destination="ping",
                        correlationId="gold-ping"))), 2)
                    last_ping = loop.time()
    except asyncio.CancelledError:
        emit("STREAM_CANCELLED")
        raise
    except FeedError as exc:
        emit(str(exc))
        raise
    except Exception:
        emit("STREAM_CONNECTION_FAILED")
        raise FeedError("STREAM_CONNECTION_FAILED") from None
    finally:
        auth.clear()
        emit("STREAM_CLOSED")
