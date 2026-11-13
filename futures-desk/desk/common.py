from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> Any:
        with self._lock:
            self._data[key] = (time.time() + ttl, value)
        return value


cache = TTLCache()


def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 25, retries: int = 3) -> bytes:
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (404, 403):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET failed {url[:90]}: {last}")


def http_json(url: str, headers: dict[str, str] | None = None, **kw: Any) -> Any:
    return json.loads(http_get(url, headers, **kw).decode("utf-8"))


def http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    h = {"User-Agent": UA, "Content-Type": "application/json", "Origin": "https://www.tradingview.com"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
