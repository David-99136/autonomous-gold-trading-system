"""Capital.com Demo 唯讀探測。URL 固定；沒有 Live 或 POST 訂單路徑。"""
import json
import re
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote


class NoRedirect(HTTPRedirectHandler):
    # 不將具交易權限的 session headers 傳給重新導向的其他網域。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CapitalDemo:
    BASE = "https://demo-api-capital.backend-capital.com/api/v1"

    def __init__(self):
        self._headers = {}
        self._opener = build_opener(NoRedirect)
        self._last = 0.0

    def _request(self, method, path, body=None):
        # 保守串列限流 5 req/s；認證與探測不與交易共享此物件。
        time.sleep(max(0, 0.2 - (time.monotonic() - self._last)))
        self._last = time.monotonic()
        req = Request(self.BASE + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={**self._headers, "Content-Type": "application/json"}, method=method)
        try:
            with self._opener.open(req, timeout=10) as response:
                return json.load(response), response.headers
        except HTTPError as exc:
            # 只取機器錯誤代碼，不輸出 body、headers 或登入內容。
            code = "UNAVAILABLE"
            try:
                payload = json.loads(exc.read(8192))
                candidate = payload.get("errorCode") if isinstance(payload, dict) else None
                secrets = [*self._headers.values(), *(body or {}).values()]
                if (isinstance(candidate, str)
                        and re.fullmatch(r"error(?:[.][A-Za-z0-9_-]+){1,12}", candidate)
                        and len(candidate) <= 160
                        and not any(isinstance(s, str) and s and s in candidate for s in secrets)):
                    code = candidate
            except (ValueError, OSError):
                pass
            finally:
                exc.close()
            raise RuntimeError(f"Capital Demo HTTP {exc.code}; errorCode={code}; credentials suppressed") from None
        except (URLError, TimeoutError):
            raise RuntimeError("Capital Demo transport failure; no automatic write retry") from None

    def login(self, credentials):
        self._headers = {"X-CAP-API-KEY": credentials["api_key"]}
        try:
            _, headers = self._request("POST", "/session", {
                "identifier": credentials["identifier"], "password": credentials["password"],
                "encryptedPassword": False})
            if not headers.get("CST") or not headers.get("X-SECURITY-TOKEN"):
                raise RuntimeError("Missing authentication headers")
            self._headers = {"CST": headers["CST"], "X-SECURITY-TOKEN": headers["X-SECURITY-TOKEN"]}
        except Exception:
            self._headers.clear()
            raise

    def discover(self):
        return self._request("GET", "/markets?" + urlencode({"searchTerm": "Gold"}))[0]

    def market(self, epic):
        return self._request("GET", "/markets/" + quote(epic, safe=""))[0]

    def prices(self, epic, resolution="MINUTE", start=None, end=None):
        if resolution not in ("MINUTE", "MINUTE_5", "HOUR"):
            raise ValueError("Unsupported resolution")
        params = {"resolution": resolution, "max": 1000}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        return self._request("GET", "/prices/" + quote(epic, safe="") + "?" + urlencode(params))[0]

    def close(self):
        self._headers.clear()


def credential_backend():
    # 指定 Windows 後端；拒絕 keyring 自動退回明文儲存。
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def save_credentials():
    from getpass import getpass
    backend = credential_backend()
    for field in ("identifier", "api_key", "password"):
        value = getpass(f"Capital Demo {field} (hidden): ")
        if not value:
            raise ValueError("Credential cannot be empty")
        backend.set_password("gold-system-capital-demo", field, value)


def load_credentials():
    backend = credential_backend()
    data = {k: backend.get_password("gold-system-capital-demo", k)
            for k in ("identifier", "api_key", "password")}
    if not all(data.values()):
        raise RuntimeError("Run credentials-set in your own terminal first")
    return data
