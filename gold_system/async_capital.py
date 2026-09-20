"""固定 Capital.com Demo URL 的非同步傳輸；預設禁止任何訂單寫入。"""
import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from urllib.parse import quote

import httpx

from .core import Rejected


DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"


def encode_flat(payload):
    """把 Decimal 輸出為 JSON number，不轉 float 或誤送成字串。"""
    fields = []
    for key, value in payload.items():
        if isinstance(value, Decimal):
            if not value.is_finite() or value <= 0 or value.adjusted() > 20 or value.as_tuple().exponent < -20:
                raise Rejected("INVALID_WIRE_NUMBER")
            encoded = format(value, "f")
        else:
            encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
        fields.append(json.dumps(key) + ":" + encoded)
    return ("{" + ",".join(fields) + "}").encode("utf-8")


class AsyncCapitalDemo:
    def __init__(self, *, transport=None, write_guard=None):
        # 可注入 MockTransport 做無網路測試。沒有 base_url、Live 或代理設定介面。
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        self._client = httpx.AsyncClient(transport=transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False, limits=limits),
                                        follow_redirects=False, trust_env=False,
                                        timeout=httpx.Timeout(1.8),
                                        limits=limits)
        self._headers = {}
        self._lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        self._last = 0.0
        self._last_login = 0.0
        self._write_guard = write_guard
        self._sent = set()

    async def _request(self, method, path, payload=None, *, login_headers=None):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise Rejected("INVALID_DEMO_PATH")
        headers = login_headers if login_headers is not None else self._headers
        if not headers:
            raise Rejected("DEMO_NOT_AUTHENTICATED")
        try:
            # HTTPX 的分段 socket timeout 不是總時限；這裡再加整個請求 deadline。
            async with asyncio.timeout(10 if login_headers is not None else 2):
                async with self._lock:
                    delay = max(0, .2 - (time.monotonic() - self._last))
                    if login_headers is not None:
                        delay = max(delay, 1 - (time.monotonic() - self._last_login))
                    await asyncio.sleep(delay)
                    self._last = time.monotonic()
                    if login_headers is not None:
                        self._last_login = self._last
                    content = encode_flat(payload) if payload is not None else None
                    async with self._client.stream(method, DEMO_BASE + path, content=content,
                            headers={**headers, "Content-Type": "application/json"}) as response:
                        if not 200 <= response.status_code < 300:
                            raise RuntimeError(f"DEMO_HTTP_{response.status_code}; response suppressed")
                        chunks, length = [], 0
                        async for chunk in response.aiter_bytes():
                            length += len(chunk)
                            if length > 1_048_576:
                                raise RuntimeError("DEMO_RESPONSE_TOO_LARGE")
                            chunks.append(chunk)
                        try:
                            data = json.loads(b"".join(chunks), parse_float=Decimal)
                        except (ValueError, UnicodeError):
                            raise RuntimeError("DEMO_INVALID_JSON") from None
                        if not isinstance(data, dict):
                            raise RuntimeError("DEMO_INVALID_SCHEMA")
                        return data, response.headers
        except (httpx.HTTPError, TimeoutError):
            raise RuntimeError("DEMO_TRANSPORT_UNCERTAIN; no automatic retry") from None
        finally:
            self._client.cookies.clear()

    async def login(self, credentials):
        self._headers.clear()
        if any(not isinstance(credentials.get(k), str) or not credentials[k]
               for k in ("api_key", "identifier", "password")):
            raise Rejected("INVALID_DEMO_CREDENTIALS")
        try:
            _, headers = await self._request("POST", "/session",
                {"identifier": credentials["identifier"], "password": credentials["password"], "encryptedPassword": False},
                login_headers={"X-CAP-API-KEY": credentials["api_key"]})
            if not headers.get("CST") or not headers.get("X-SECURITY-TOKEN"):
                raise RuntimeError("DEMO_MISSING_SESSION_HEADERS")
            self._headers = {"CST": headers["CST"], "X-SECURITY-TOKEN": headers["X-SECURITY-TOKEN"]}
        except BaseException:
            self._headers.clear()
            raise

    async def session(self):
        return (await self._request("GET", "/session"))[0]

    def quotes(self, *, on_event=None, epic="GOLD", clock=None):
        """僅 Demo login 的 session 可用；模型只收到 Quote，沒有 token。"""
        from .streaming import gold_quotes
        return gold_quotes(self._headers, on_event=on_event, epic=epic, clock=clock)

    async def preferences(self):
        return (await self._request("GET", "/accounts/preferences"))[0]

    async def positions(self):
        return (await self._request("GET", "/positions"))[0]

    async def working_orders(self):
        return (await self._request("GET", "/workingorders"))[0]

    async def activity(self):
        return (await self._request("GET", "/history/activity?lastPeriod=600&detailed=true"))[0]

    async def confirmation(self, reference):
        return (await self._request("GET", "/confirms/" + quote(reference, safe="")))[0]

    async def post_prepared_open(self, journal, intent):
        """只接受 journal 中已開始送出的意圖，且每次必須經獨立 write_guard 核准。

        正式 guard 尚未接線；預設 None 永远禁止下單。不得以 lambda True 在實際
        帳戶替代市場、風控、契約驗證與操作者 Demo 啟用流程。
        """
        async with self._order_lock:
            if self._write_guard is None:
                raise Rejected("DEMO_WRITES_DISABLED")
            if intent in self._sent:
                raise Rejected("DEMO_REQUEST_ALREADY_SENT")
            row = journal.get(intent)
            if row["state"] != "SUBMITTING":
                raise Rejected("ORDER_NOT_DURABLY_SUBMITTING")
            if await self._write_guard(journal, intent) is not True:
                raise Rejected("DEMO_RISK_GUARD_REJECTED")
            session = await self.session()
            account = session.get("accountId")
            if (not isinstance(account, str) or not account or session.get("currency") != "USD"
                    or sha256(account.encode()).hexdigest() != row["account_hash"]):
                raise Rejected("DEMO_ACCOUNT_CHANGED")
            request = row["request"]
            if request["epic"] != "GOLD" or request["direction"] not in ("BUY", "SELL"):
                raise Rejected("INVALID_DEMO_INSTRUMENT")
            expires = datetime.fromisoformat(request["expires"])
            if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                raise Rejected("DEMO_ORDER_EXPIRED")
            if journal.get(intent)["state"] != "SUBMITTING":
                raise Rejected("DEMO_ORDER_STATE_CHANGED")
            payload = {"epic": "GOLD", "direction": request["direction"],
                       "size": Decimal(request["size"]), "stopLevel": Decimal(request["stopLevel"]),
                       "profitLevel": Decimal(request["profitLevel"]),
                       "guaranteedStop": False, "trailingStop": False}
            self._sent.add(intent)  # 即使 socket 回應失敗，此物件也不重送。
            return (await self._request("POST", "/positions", payload))[0]

    async def close(self):
        self._headers.clear()
        self._write_guard = None
        await self._client.aclose()
