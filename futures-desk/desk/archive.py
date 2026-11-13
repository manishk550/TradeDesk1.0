"""Forward test for the 5-minute Opening Range Breakout.

Free sources have no multi-year intraday history, so the desk records each completed session's
5-minute bars for every contract and scores the ORB rule on what it has recorded. The evidence
therefore accumulates one real session at a time (no look-ahead, nothing fitted).
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from . import strategies
from .common import DATA

ARCH = DATA / "intraday"
MIN_RISK_PCT = 0.0015  # ignore opening candles narrower than 0.15% (R would explode)
_LOCK = threading.Lock()


def _file(market: str, day: str):
    d = ARCH / market
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{day}.json"


def recorded_days(market: str) -> list[str]:
    d = ARCH / market
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def record_if_due(market: str, service) -> dict[str, Any]:
    """Save today's/last session's 5-minute bars once the session is complete."""
    st = service.market_status(market)
    if st["state"] == "open":
        return {"recorded": False, "why": "session in progress"}
    insts = service.instruments(market)
    if not insts:
        return {"recorded": False, "why": "no universe"}
    probe = "RELIANCE" if market == "IN" else "NVDA"
    probe = probe if any(i["id"] == probe for i in insts) else insts[0]["id"]
    tz = service.MARKETS[market]["tz"]
    try:
        r = service.intraday_raw(market, probe)
    except Exception as exc:  # noqa: BLE001
        return {"recorded": False, "why": str(exc)}
    if not r:
        return {"recorded": False, "why": "no bars"}
    last = datetime.fromtimestamp(r[-1]["time"], tz)
    day = last.date().isoformat()
    close_t = service.MARKETS[market]["close"]
    close_dt = datetime.combine(last.date(), close_t, tzinfo=tz)
    if (close_dt - last).total_seconds() > 20 * 60:
        return {"recorded": False, "why": "session not complete in feed"}
    f = _file(market, day)
    if f.exists():
        return {"recorded": False, "why": "already recorded", "day": day}
    with _LOCK:
        if f.exists():
            return {"recorded": False, "why": "already recorded", "day": day}
        out: dict[str, list] = {}

        def one(inst: dict[str, Any]) -> None:
            try:
                bars = service.intraday_raw(market, inst["id"])
                if bars:
                    out[inst["id"]] = [[b["time"], b["open"], b["high"], b["low"], b["close"]] for b in bars]
            except Exception:  # noqa: BLE001
                pass

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, insts))
        if len(out) >= 10:
            f.write_text(json.dumps(out))
            return {"recorded": True, "day": day, "symbols": len(out)}
    return {"recorded": False, "why": "too few symbols fetched"}


def _orb_trade(bars: list[list], cost: float) -> dict[str, Any] | None:
    if len(bars) < 6:
        return None
    t, o, h, lo, c = bars[0]
    if c == o:
        return None
    side = 1 if c > o else -1
    trig, stop = (h, lo) if side > 0 else (lo, h)
    risk = abs(trig - stop)
    if risk < MIN_RISK_PCT * trig:
        return None
    entry = None
    for k in range(1, len(bars)):
        _, bo, bh, bl, bc = bars[k]
        if entry is None:
            if (side > 0 and bh > trig) or (side < 0 and bl < trig):
                entry = trig
                # pessimistic: if the breakout bar also touched the stop, count the stop
                if (side > 0 and bl <= stop) or (side < 0 and bh >= stop):
                    return {"r": (-risk - cost * entry) / risk, "side": side, "why": "stop"}
        else:
            if (side > 0 and bl <= stop) or (side < 0 and bh >= stop):
                return {"r": (-risk - cost * entry) / risk, "side": side, "why": "stop"}
    if entry is None:
        return None
    exit_px = bars[-1][4]
    return {"r": ((exit_px - entry) * side - cost * entry) / risk, "side": side, "why": "close"}


def orb_forward(market: str, ctxs: dict[str, dict]) -> dict[str, Any]:
    cost = strategies.COST[market]
    days = recorded_days(market)
    all_r: list[float] = []
    all_d: list[str] = []
    ip_r: list[float] = []
    ip_d: list[str] = []
    for day in days:
        data = json.loads(_file(market, day).read_text())
        for sym, bars in data.items():
            tr = _orb_trade(bars, cost)
            if not tr:
                continue
            all_r.append(max(tr["r"], -3.0))
            all_d.append(day)
            ctx = ctxs.get(sym)
            if not ctx:
                continue
            # "in play" proxy known at the open: gap of at least 0.5 ATR vs the previous close
            idx = next((i for i, b in enumerate(ctx["bars"]) if b["date"] == day), None)
            prev = idx - 1 if idx else ctx["n"] - 1 if ctx["bars"][-1]["date"] < day else None
            if prev is None or prev < 0:
                continue
            atr = ctx["S"]["atr"][prev]
            if atr and abs(bars[0][1] - ctx["bars"][prev]["close"]) >= 0.5 * atr:
                ip_r.append(max(tr["r"], -3.0))
                ip_d.append(day)
    return {
        "days": len(days),
        "first": days[0] if days else None,
        "last": days[-1] if days else None,
        "all": strategies.stats(all_r, all_d),
        "in_play": strategies.stats(ip_r, ip_d),
        "need_days": 60,
    }
