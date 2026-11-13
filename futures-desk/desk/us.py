"""US names on the CoinDCX Global Futures list: Nasdaq daily history + intraday ticks."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .common import DATA, ROOT, http_json

BASE = "https://api.nasdaq.com/api/quote"
H = {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
US_DIR = DATA / "us"
UNIVERSE_FILE = ROOT / "universe_us.json"
NY = ZoneInfo("America/New_York")

STATUS: dict[str, Any] = {"running": False, "done": 0, "total": 0, "failed": []}
BAD: dict[str, float] = {}  # symbols with no usable history -> retry after 6 hours


def _money(v: Any) -> float | None:
    if v is None:
        return None
    t = str(v).replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip()
    if not t or t in {"--", "N/A"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def universe() -> list[dict[str, Any]]:
    return json.loads(UNIVERSE_FILE.read_text())["instruments"]


def _fetch_daily(sym: str, asset: str, years: int = 2) -> list[dict[str, Any]]:
    now = datetime.utcnow()
    start = now - timedelta(days=365 * years + 5)
    q = urllib.parse.urlencode(
        {
            "assetclass": asset,
            "fromdate": start.strftime("%Y-%m-%d"),
            "todate": now.strftime("%Y-%m-%d"),
            "limit": "800",
        }
    )
    data = http_json(f"{BASE}/{urllib.parse.quote(sym, safe='')}/historical?{q}", H)
    rows = ((data.get("data") or {}).get("tradesTable") or {}).get("rows") or []
    bars = []
    for r in reversed(rows):
        c = _money(r.get("close"))
        if c is None:
            continue
        try:
            d = datetime.strptime(r.get("date") or "", "%m/%d/%Y")
        except ValueError:
            continue
        bars.append(
            {
                "time": int(d.replace(hour=16).timestamp()),
                "date": d.date().isoformat(),
                "open": _money(r.get("open")) or c,
                "high": _money(r.get("high")) or c,
                "low": _money(r.get("low")) or c,
                "close": c,
                "volume": _money(r.get("volume")) or 0.0,
            }
        )
    return bars


def daily(inst: dict[str, Any], max_age_h: float = 6.0) -> list[dict[str, Any]]:
    sym = inst["id"]
    path = US_DIR / f"{sym}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_h * 3600:
        return json.loads(path.read_text())
    if sym in BAD and time.time() - BAD[sym] < 6 * 3600 and not path.exists():
        raise RuntimeError(f"no US history for {sym} (cached failure)")
    last_err: Exception | None = None
    for asset in ([inst.get("asset")] if inst.get("asset") else ["stocks", "etf"]):
        try:
            bars = _fetch_daily(inst.get("nasdaq") or sym, asset)
            if len(bars) >= 60:
                path.write_text(json.dumps(bars))
                inst["asset"] = asset
                return bars
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if path.exists():
        return json.loads(path.read_text())
    BAD[sym] = time.time()
    raise RuntimeError(f"no US history for {sym}: {last_err}")


def sync_all(max_age_h: float = 6.0) -> None:
    if STATUS["running"]:
        return
    insts = universe()
    STATUS.update(running=True, done=0, total=len(insts), failed=[])

    def one(inst: dict[str, Any]) -> None:
        try:
            daily(inst, max_age_h)
        except Exception:  # noqa: BLE001
            STATUS["failed"].append(inst["id"])
        STATUS["done"] += 1
        time.sleep(0.05)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, insts))
    finally:
        STATUS["running"] = False


def start_sync_thread() -> None:
    if not STATUS["running"]:
        threading.Thread(target=sync_all, daemon=True, name="us-sync").start()


def intraday(sym: str, asset: str = "stocks") -> list[dict[str, Any]]:
    """1-minute ticks -> 5-minute OHLC bars (US regular session)."""
    data = http_json(f"{BASE}/{urllib.parse.quote(sym, safe='')}/chart?assetclass={asset}", H)
    pts = ((data.get("data") or {}).get("chart")) or []
    buckets: dict[int, dict[str, Any]] = {}
    for p in pts:
        z = p.get("z") or {}
        price = _money(z.get("value")) or _money(p.get("y"))
        ts = p.get("x")
        if price is None or ts is None:
            continue
        # Nasdaq sends Eastern wall-clock time labelled as UTC; convert to a real epoch
        wall = datetime.utcfromtimestamp(int(ts) / 1000)
        sec = int(wall.replace(tzinfo=NY).timestamp())
        b = sec - sec % 300
        cur = buckets.get(b)
        vol = _money(z.get("volume")) or 0.0
        if cur is None:
            buckets[b] = {"time": b, "open": price, "high": price, "low": price, "close": price, "volume": vol}
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            cur["volume"] += vol
    return [buckets[k] for k in sorted(buckets)]
