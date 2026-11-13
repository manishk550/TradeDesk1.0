"""TradingView scanner: live snapshot, technical rating and fundamentals in one call per market."""
from __future__ import annotations

import re
from typing import Any

from .common import cache, http_post_json

COLS = [
    "name", "description", "close", "open", "high", "low", "change", "gap", "volume",
    "relative_volume_10d_calc", "average_volume_30d_calc", "premarket_change",
    "RSI", "ADX", "MACD.macd", "MACD.signal", "Recommend.All", "Recommend.MA", "Recommend.Other",
    "Perf.W", "Perf.1M", "Perf.3M", "Perf.YTD", "price_52_week_high", "price_52_week_low", "beta_1_year",
    "market_cap_basic", "price_earnings_ttm", "earnings_per_share_diluted_ttm", "return_on_equity",
    "debt_to_equity", "dividends_yield", "net_margin_ttm", "gross_margin_ttm", "total_revenue_yoy_growth_ttm",
    "earnings_per_share_diluted_yoy_growth_ttm", "price_book_fq", "current_ratio", "sector", "industry",
    "earnings_release_next_date", "exchange",
]
IDX = {c: i for i, c in enumerate(COLS)}
SCAN = {"IN": "https://scanner.tradingview.com/india/scan", "US": "https://scanner.tradingview.com/america/scan"}


def tv_name(sym: str) -> str:
    return re.sub(r"[&\-]", "_", sym)


def snapshot(market: str, symbols: list[str]) -> dict[str, dict[str, Any]]:
    key = f"tv:{market}:{len(symbols)}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    names = sorted({tv_name(s) for s in symbols})
    out: dict[str, dict[str, Any]] = {}
    for a in range(0, len(names), 150):
        chunk = names[a : a + 150]
        payload = {
            "filter": [{"left": "name", "operation": "in_range", "right": chunk}],
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": COLS,
            "range": [0, 400],
        }
        try:
            data = http_post_json(SCAN[market], payload)
        except Exception:  # noqa: BLE001
            continue
        for row in data.get("data") or []:
            d = dict(zip(COLS, row["d"]))
            d["tv_symbol"] = row["s"]
            nm = d["name"]
            # prefer primary listings (NSE / NASDAQ / NYSE) when duplicates exist
            if nm in out and (d.get("exchange") or "") not in ("NSE", "NASDAQ", "NYSE", "AMEX", "NYSE ARCA"):
                continue
            out[nm] = d
    return cache.set(key, out, 90)


def lookup(snap: dict[str, dict[str, Any]], sym: str) -> dict[str, Any]:
    return snap.get(tv_name(sym)) or snap.get(sym) or {}


def rating_label(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 0.5:
        return "Strong Buy"
    if v >= 0.1:
        return "Buy"
    if v > -0.1:
        return "Neutral"
    if v > -0.5:
        return "Sell"
    return "Strong Sell"


def fundamentals(d: dict[str, Any]) -> dict[str, Any]:
    """Compact fundamentals block + a simple quality read used as a tie-breaker (never as a trigger)."""
    if not d:
        return {"available": False}
    pe, roe, de = d.get("price_earnings_ttm"), d.get("return_on_equity"), d.get("debt_to_equity")
    growth, margin = d.get("total_revenue_yoy_growth_ttm"), d.get("net_margin_ttm")
    pts, notes = 0, []
    if roe is not None:
        if roe >= 15:
            pts += 1
            notes.append(f"ROE {roe:.0f}% strong")
        elif roe < 5:
            pts -= 1
            notes.append(f"ROE {roe:.0f}% weak")
    if de is not None:
        if de <= 0.6:
            pts += 1
            notes.append("low debt")
        elif de > 2:
            pts -= 1
            notes.append(f"debt/equity {de:.1f} high")
    if growth is not None:
        if growth >= 10:
            pts += 1
            notes.append(f"revenue {growth:+.0f}% y/y")
        elif growth < 0:
            pts -= 1
            notes.append(f"revenue {growth:+.0f}% y/y")
    if margin is not None and margin < 0:
        pts -= 1
        notes.append("loss-making")
    if pe is not None and pe > 80:
        notes.append(f"P/E {pe:.0f} rich")
    hi, lo, px = d.get("price_52_week_high"), d.get("price_52_week_low"), d.get("close")
    pos = None
    if hi and lo and px and hi > lo:
        pos = round(100 * (px - lo) / (hi - lo))
    return {
        "available": True,
        "sector": d.get("sector"),
        "industry": d.get("industry"),
        "market_cap": d.get("market_cap_basic"),
        "pe": pe,
        "eps": d.get("earnings_per_share_diluted_ttm"),
        "roe": roe,
        "debt_equity": de,
        "div_yield": d.get("dividends_yield"),
        "net_margin": margin,
        "revenue_growth": growth,
        "eps_growth": d.get("earnings_per_share_diluted_yoy_growth_ttm"),
        "pb": d.get("price_book_fq"),
        "beta": d.get("beta_1_year"),
        "range_pos": pos,
        "high_52w": hi,
        "low_52w": lo,
        "perf": {"1W": d.get("Perf.W"), "1M": d.get("Perf.1M"), "3M": d.get("Perf.3M"), "YTD": d.get("Perf.YTD")},
        "quality": "strong" if pts >= 2 else ("weak" if pts <= -1 else "ok"),
        "quality_pts": pts,
        "notes": notes,
        "next_earnings": d.get("earnings_release_next_date"),
    }
