"""固定 Demo、受限方法的診斷 Adapter；不重用交易端的寫入能力。

[Author: Antigravity | Date: 2026-09-27]
"""
from hashlib import sha256

import httpx

from .async_capital import DEMO_BASE
from .sampler_plan import SampleError, strict_json, utc


class DemoHistoryReader:
    def __init__(self, credentials, *, transport=None):
        self._credentials = dict(credentials)
        self._headers = {}
        self._login_attempted = False
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False, limits=limits),
            timeout=httpx.Timeout(1.8), follow_redirects=False, trust_env=False, limits=limits)

    async def send(self, kind, query, receipt, mark):
        """receipt 只填固定欄位；mark 由採樣器記錄真實接收階段。"""
        if kind not in ("SESSION", "TIME", "MARKET", "PRICES"):
            raise SampleError("READ_ENDPOINT_FORBIDDEN")
        if kind != "PRICES" and query:
            raise SampleError("READ_QUERY_FORBIDDEN")
        method, payload, params = "GET", None, None
        headers = self._headers
        path = {"SESSION": "/session", "TIME": "/time", "MARKET": "/markets/GOLD",
                "PRICES": "/prices/GOLD"}[kind]
        if kind == "SESSION":
            if self._login_attempted:
                raise SampleError("RELOGIN_FORBIDDEN")
            self._login_attempted = True
            if any(not isinstance(self._credentials.get(k), str) or not self._credentials[k]
                   for k in ("api_key", "identifier", "password")):
                raise SampleError("CREDENTIALS_INVALID")
            method = "POST"
            headers = {"X-CAP-API-KEY": self._credentials["api_key"]}
            payload = dict(identifier=self._credentials["identifier"], password=self._credentials["password"],
                           encryptedPassword=False)
        elif not headers:
            raise SampleError("DEMO_NOT_AUTHENTICATED")
        if kind == "PRICES":
            if (not isinstance(query, dict) or set(query) != {"resolution", "from", "to", "max"}
                    or query["resolution"] not in ("MINUTE", "MINUTE_5", "HOUR")
                    or type(query["max"]) is not int or not 1 <= query["max"] <= 1000):
                raise SampleError("READ_QUERY_FORBIDDEN")
            left, right = utc(query["from"]), utc(query["to"])
            if not 0 <= (right-left).total_seconds() <= 999*60 or any(x.second or x.microsecond for x in (left, right)):
                raise SampleError("READ_QUERY_FORBIDDEN")
            params = dict(query, **{"from": left.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "to": right.strftime("%Y-%m-%dT%H:%M:%S")})
        try:
            async with self._client.stream(method, DEMO_BASE+path, params=params, json=payload, headers=headers) as response:
                receipt["http_status"] = response.status_code
                mark("headers_received")
                if not 200 <= response.status_code < 300:
                    raise SampleError("HTTP_REJECTED")
                parts, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_048_576:
                        raise SampleError("RESPONSE_SIZE_LIMIT")
                    parts.append(chunk)
                raw = b"".join(parts)
                receipt["body_sha256"] = sha256(raw).hexdigest()
                mark("body_complete")
                try:
                    data = strict_json(raw)
                except Exception:
                    raise SampleError("RESPONSE_JSON_INVALID") from None
                if not isinstance(data, dict):
                    raise SampleError("RESPONSE_SCHEMA_INVALID")
                if kind == "SESSION":
                    if not response.headers.get("CST") or not response.headers.get("X-SECURITY-TOKEN"):
                        raise SampleError("SESSION_HEADERS_MISSING")
                    self._headers = {k: response.headers[k] for k in ("CST", "X-SECURITY-TOKEN")}
                    return {}  # 登入 body／headers 絕不交給檔案記錄器。
                return data
        except httpx.HTTPError:
            raise SampleError("READ_TRANSPORT_FAILED") from None
        finally:
            self._client.cookies.clear()
            if kind == "SESSION":
                self._credentials.clear()

    async def close(self):
        self._credentials.clear()
        self._headers.clear()
        await self._client.aclose()
