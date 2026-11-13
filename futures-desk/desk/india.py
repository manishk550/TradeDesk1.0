"""NSE F&O futures data: daily bhavcopy -> continuous front-month series with open interest."""
from __future__ import annotations

import csv
import io
import json
import threading
import time
import urllib.error
import zipfile
from datetime import date, datetime, timedelta
from typing import Any

from .common import DATA, http_get

BHAV = DATA / "bhav"
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
HEADERS = {"Referer": "https://www.nseindia.com/"}
HISTORY_DAYS = 760  # calendar days (~520 sessions)

STATUS: dict[str, Any] = {"running": False, "done": 0, "total": 0, "last": None, "error": None}
_LOCK = threading.Lock()


def _day_file(d: date):
    return BHAV / f"{d.strftime('%Y%m%d')}.json"


def _fnum(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fetch_day(d: date) -> list[list] | None:
    """Return futures rows for a session or None when NSE has no file (weekend/holiday)."""
    path = _day_file(d)
    if path.exists():
        return json.loads(path.read_text())
    try:
        raw = http_get(URL.format(d=d.strftime("%Y%m%d")), HEADERS, timeout=60, retries=2)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 403):
            path.write_text("null")
            return None
        raise
    zf = zipfile.ZipFile(io.BytesIO(raw))
    text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    rows: list[list] = []
    for r in csv.DictReader(io.StringIO(text)):
        tp = r.get("FinInstrmTp")
        if tp not in ("STF", "IDF"):
            continue
        rows.append(
            [
                r["TckrSymb"].strip(),
                tp,
                r["XpryDt"],
                _fnum(r["OpnPric"]),
                _fnum(r["HghPric"]),
                _fnum(r["LwPric"]),
                _fnum(r["ClsPric"]),
                _fnum(r["PrvsClsgPric"]),
                _fnum(r["OpnIntrst"]),
                _fnum(r["ChngInOpnIntrst"]),
                _fnum(r["TtlTradgVol"]),
                _fnum(r["UndrlygPric"]),
                int(_fnum(r["NewBrdLotQty"])),
                _fnum(r["TtlTrfVal"]),
            ]
        )
    path.write_text(json.dumps(rows))
    return rows


def sync(days: int = HISTORY_DAYS) -> None:
    """Download any missing sessions. Safe to call repeatedly; runs in the caller's thread."""
    with _LOCK:
        if STATUS["running"]:
            return
        STATUS.update(running=True, done=0, error=None)
    try:
        today = date.today()
        wanted = [today - timedelta(days=i) for i in range(days) if (today - timedelta(days=i)).weekday() < 5]
        wanted.sort()
        STATUS["total"] = len(wanted)
        for i, d in enumerate(wanted):
            path = _day_file(d)
            # always retry "today" and yesterday (file may appear later in the evening)
            if path.exists() and path.read_text() == "null" and (today - d).days <= 1:
                path.unlink()
            if not path.exists():
                try:
                    fetch_day(d)
                except Exception as exc:  # network blip; keep going
                    STATUS["error"] = str(exc)
                time.sleep(0.15)
            STATUS["done"] = i + 1
            STATUS["last"] = d.isoformat()
    finally:
        STATUS["running"] = False
        invalidate()


def start_sync_thread() -> None:
    if STATUS["running"]:
        return
    threading.Thread(target=sync, daemon=True, name="bhav-sync").start()


_SERIES: dict[str, Any] = {"built": 0.0, "data": None}


def invalidate() -> None:
    _SERIES["built"] = 0.0


def sessions() -> list[tuple[date, list[list]]]:
    out = []
    for p in sorted(BHAV.glob("*.json")):
        txt = p.read_text()
        if txt == "null":
            continue
        rows = json.loads(txt)
        if rows:
            out.append((datetime.strptime(p.stem, "%Y%m%d").date(), rows))
    return out


def _parse_exp(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def build_series() -> dict[str, dict[str, Any]]:
    """Per symbol: roll-adjusted front-month OHLC (daily) + OI, change in OI, lot size, turnover."""
    if _SERIES["data"] is not None and time.time() - _SERIES["built"] < 600:
        return _SERIES["data"]
    per: dict[str, list[dict[str, Any]]] = {}
    for d, rows in sessions():
        front: dict[str, list] = {}
        for r in rows:
            sym, tp, exp = r[0], r[1], r[2]
            if r[6] <= 0:
                continue
            cur = front.get(sym)
            if cur is None or exp < cur[2]:
                front[sym] = r
        for sym, r in front.items():
            if r[10] <= 0 and r[3] <= 0:
                continue
            o = r[3] or r[6]
            h = r[4] or max(o, r[6])
            lo = r[5] or min(o, r[6])
            per.setdefault(sym, []).append(
                {
                    "date": d,
                    "exp": r[2],
                    "kind": "index" if r[1] == "IDF" else "stock",
                    "o": o,
                    "h": h,
                    "l": lo,
                    "c": r[6],
                    "pc": r[7] or r[6],
                    "oi": r[8],
                    "doi": r[9],
                    "v": r[10],
                    "und": r[11],
                    "lot": r[12],
                    "tv": r[13],
                }
            )
    out: dict[str, dict[str, Any]] = {}
    for sym, rows in per.items():
        if len(rows) < 40:
            continue
        # chain-adjust: each day's candle is scaled so that prev close of day t == adj close of day t-1
        adj_prev = None
        bars = []
        for r in rows:
            if adj_prev is None:
                k = 1.0
            else:
                k = adj_prev / r["pc"] if r["pc"] else 1.0
            ac = r["c"] * k
            bars.append(
                {
                    "time": int(datetime(r["date"].year, r["date"].month, r["date"].day, 10, 0).timestamp()),
                    "date": r["date"].isoformat(),
                    "open": r["o"] * k,
                    "high": r["h"] * k,
                    "low": r["l"] * k,
                    "close": ac,
                    "volume": r["v"],
                    "oi": r["oi"],
                    "doi": r["doi"],
                    "raw_close": r["c"],
                }
            )
            adj_prev = ac
        last = rows[-1]
        # back-adjust to the present so the final bar equals the real tradable contract price
        f = last["c"] / bars[-1]["close"] if bars[-1]["close"] else 1.0
        for b in bars:
            for k in ("open", "high", "low", "close"):
                b[k] *= f
        out[sym] = {
            "symbol": sym,
            "kind": last["kind"],
            "lot": last["lot"],
            "expiry": last["exp"],
            "last_close": last["c"],
            "underlying": last["und"],
            "turnover": last["tv"],
            "bars": bars,
        }
    _SERIES["data"] = out
    _SERIES["built"] = time.time()
    return out


def universe(min_turnover: float = 5e8) -> list[dict[str, Any]]:
    """Liquid F&O names (last session turnover) + all index futures."""
    data = build_series()
    rows = []
    for sym, s in data.items():
        if s["kind"] != "index" and s["turnover"] < min_turnover:
            continue
        rows.append({"id": sym, "name": sym, "kind": s["kind"], "lot": s["lot"], "expiry": s["expiry"]})
    return rows
